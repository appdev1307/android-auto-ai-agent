"""Code-aware chunking for Java/Kotlin/C++/AIDL/VSS YAML."""

from __future__ import annotations
import re
from pathlib import Path
from typing import Iterator

CODE_EXTS = {
    ".java", ".kt", ".kts", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".aidl", ".hal", ".bp", ".xml", ".yaml", ".yml", ".json", ".vss",
}

# ── HIDL exclusion ───────────────────────────────────────────────
# HIDL is legacy (pre-AIDL) HAL. For Android 14+ AAOS we want AIDL only —
# retrieving HIDL teaches the model wrong patterns (V2_0 namespaces, .hal).
# Discriminate by PATH, never by content/filename: HIDL and AIDL share
# identifiers (IVehicle, VehiclePropertyStore, "vehicle"), so a keyword
# filter wrongly drops the canonical AIDL reference impl. Path is exact.
HIDL_PATH_MARKERS = (
    "/2.0/", "/1.0/", "/3.0/", "/4.0/", "/hidl/", "/hidl-generated/",
    "/vehicle/2.0/", "/vehicle/1.0/", "/v2_0/", "/v1_0/", "/v3_0/",
)


def is_hidl(path: str, content: str | None = None) -> bool:
    """True if a file is legacy HIDL.

    Path markers cover AOSP upstream. Vendor/OEM trees can ship a HIDL HAL
    in a non-standard path, so two extra signals — safe because neither
    `.hal` nor `hidl_interface {` ever appears in an AIDL file (unlike the
    shared identifiers a keyword filter would false-positive on):
      - a `.hal` extension (AIDL uses `.aidl`)
      - a `hidl_interface { }` Soong module in an Android.bp
    """
    p = path.replace("\\", "/").lower()
    if any(m in p for m in HIDL_PATH_MARKERS):
        return True
    if p.endswith(".hal"):
        return True
    if p.endswith(".bp") and content and "hidl_interface" in content:
        return True
    return False

# ── Aggressive AOSP index filter ─────────────────────────────────
# Full AOSP is ~500GB / ~1M files, but >90% is not worth vectorizing.
# We drop it at index time so embedding cost stays sane. Order of checks
# is cheapest-first (path substring) before any file I/O.

# Directory names that mean "not real source" — matched as a path segment.
EXCLUDE_DIR_SEGMENTS = {
    "out", "out_host", ".git", ".repo", "prebuilts", "prebuilt",
    "node_modules", "test", "tests", "testing", "androidtest", "cts",
    "vts", "gts", "benchmarks", "fuzz", "fuzzing", "docs", "doc",
    "samples", "sample", "examples", "example", "third_party",
    "external",            # huge; upstream libs, rarely the bug site
    "toolchain", "clang", "gcc", "jdk", "kotlinc", "ndk",
    ".gradle", "build", "intermediates", "gen", "generated",
    "__pycache__", ".idea",
}

# Path substrings that flag generated / vendored blobs anywhere in the path.
EXCLUDE_PATH_SUBSTR = (
    "/generated/", "/gen/", "_gen/", ".pb.", "protobuf-gen",
    "/aidl_api/",          # frozen AIDL snapshots (dupes of the live .aidl)
    "/mockito", "/junit", "/gtest", "/googletest",
)

# Filename suffixes that are never a useful source chunk.
EXCLUDE_NAME_SUFFIX = (
    "test.java", "tests.java", "test.kt", "test.cpp", "_test.cc",
    "test.py", "_pb2.py", ".pb.h", ".pb.cc",
)

# Cap: skip absurdly large files (usually generated tables / minified blobs).
MAX_INDEX_FILE_BYTES = 400_000   # ~400 KB

# Customer tier keeps almost everything — OEM patches land anywhere (even in
# test/, external/, generated/), and every file there is IP worth indexing.
# Only hard junk is dropped.
CUSTOMER_HARD_EXCLUDE_SEGMENTS = {"out", "out_host", ".git", ".repo",
                                  "node_modules", "__pycache__", ".gradle"}


def should_index(path: Path, mode: str = "base") -> bool:
    """Cheap, decisive filter. False => don't index this file.

    mode="base"     : aggressive (AOSP upstream — drop tests/prebuilts/external/…)
    mode="customer" : permissive (OEM overlay — keep tests/external/generated,
                      drop only obvious junk). OEM patches are the whole point.
    """
    p = path.as_posix().lower()
    parts = {seg.lower() for seg in path.parts}

    # common gates (both modes)
    if path.suffix.lower() not in CODE_EXTS:
        return False
    if is_hidl(str(path)):
        return False
    try:
        if path.stat().st_size > MAX_INDEX_FILE_BYTES:
            return False
    except OSError:
        return False

    if mode == "customer":
        # keep everything except hard junk
        return not (parts & CUSTOMER_HARD_EXCLUDE_SEGMENTS)

    # base mode: aggressive
    if parts & EXCLUDE_DIR_SEGMENTS:
        return False
    if any(s in p for s in EXCLUDE_PATH_SUBSTR):
        return False
    if any(p.endswith(s) for s in EXCLUDE_NAME_SUFFIX):
        return False
    return True


# Rough structural splits
_SPLIT_RE = re.compile(
    r"(?m)^(?:"
    r"\s*(?:public|private|protected|static|final|abstract|native|\s)*"
    r"(?:class|interface|object|fun|func|void|int|bool|string|struct|enum)\s+\w+"
    r"|property\s*\(|signal\s+|Vehicle\.|\.aidl\b"
    r")"
)


def iter_files(root: Path, extra_roots: list[str] | None = None,
               mode: str = "base") -> Iterator[Path]:
    roots = [root]
    if extra_roots:
        for r in extra_roots:
            p = root / r if not Path(r).is_absolute() else Path(r)
            if p.exists():
                roots.append(p)
    seen = set()
    for base in roots:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if not should_index(path, mode=mode):
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            yield path


# ── AST-based chunking (tree-sitter) ─────────────────────────────
# Cut code at real semantic units (whole methods/functions) instead of at text
# boundaries, and prefix each chunk with its structural path (Class.method) so
# retrieval sees where the code lives — better localization than regex splits.
# Java + C/C++ only; everything else (.aidl, .bp, .yaml, .json, ...) and any
# parse failure fall back to the regex chunker. Degrades silently if the
# tree-sitter grammars aren't installed.
_TS = {"tried": False, "java": None, "cpp": None, "kotlin": None}

_AST_LANG = {
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".c": "cpp", ".h": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
}
_UNIT_TYPES = {
    "method_declaration", "constructor_declaration",   # java
    "function_definition",                             # cpp
    "function_declaration", "secondary_constructor",   # kotlin
}
_SCOPE_TYPES = {
    "class_declaration", "interface_declaration", "enum_declaration",   # java
    "class_specifier", "struct_specifier", "namespace_definition",      # cpp
    "object_declaration",                                               # kotlin (class_declaration shared)
}


def _get_parser(lang: str):
    if not _TS["tried"]:
        _TS["tried"] = True
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_java as tsj
            import tree_sitter_cpp as tsc
            _TS["java"] = Parser(Language(tsj.language()))
            _TS["cpp"] = Parser(Language(tsc.language()))
            try:
                import tree_sitter_kotlin as tsk
                _TS["kotlin"] = Parser(Language(tsk.language()))
            except Exception:
                _TS["kotlin"] = None
        except Exception:
            _TS["java"] = _TS["cpp"] = _TS["kotlin"] = None
    return _TS.get(lang)


def _node_name(node) -> str:
    n = node.child_by_field_name("name")
    if n is not None and n.text:
        return n.text.decode("utf-8", "ignore")
    # C/C++ functions: follow the declarator chain to the name,
    # WITHOUT descending into the parameter list.
    d = node.child_by_field_name("declarator")
    guard = 0
    while d is not None and guard < 12:
        guard += 1
        if d.type in ("identifier", "field_identifier", "qualified_identifier",
                      "operator_name", "destructor_name") and d.text:
            return d.text.decode("utf-8", "ignore")
        nd = d.child_by_field_name("declarator")
        if nd is None:
            for ch in d.children:
                if ch.type in ("identifier", "field_identifier", "qualified_identifier") and ch.text:
                    return ch.text.decode("utf-8", "ignore")
            break
        d = nd
    return ""


def _collect_units(node, stack: list[str], out: list[tuple]):
    t = node.type
    if t in _UNIT_TYPES:
        name = _node_name(node)
        qn = ".".join([s for s in stack if s] + ([name] if name else []))
        out.append((qn, node.start_byte, node.end_byte))
        return  # keep the whole unit; don't split into nested units
    if t in _SCOPE_TYPES:
        stack = stack + [_node_name(node)]
    for ch in node.children:
        _collect_units(ch, stack, out)


def _ast_chunks(path: Path, text: str, chunk_size: int, overlap: int) -> list[dict] | None:
    lang = _AST_LANG.get(path.suffix.lower())
    if not lang:
        return None
    parser = _get_parser(lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(bytes(text, "utf-8"))
    except Exception:
        return None
    units: list[tuple] = []
    _collect_units(tree.root_node, [], units)
    if not units:
        return None
    src = text.encode("utf-8")
    chunks: list[dict] = []
    for qn, s, e in units:
        code = src[s:e].decode("utf-8", "ignore")
        header = f"// {path.name} :: {qn}\n" if qn else ""
        if len(code) <= int(chunk_size * 1.5):
            chunks.append(_make_chunk(path, header + code, len(chunks)))
        else:
            start = 0
            while start < len(code):
                end = min(len(code), start + chunk_size)
                chunks.append(_make_chunk(path, header + code[start:end], len(chunks)))
                if end >= len(code):
                    break
                start = max(end - overlap, start + 1)
    return chunks


# ── Syntax oracle: tree-sitter parse-check (Update: real parser, not guessing) ──
# Detect syntax errors by PARSING with tree-sitter and looking for ERROR /
# MISSING nodes — the C4 "validate" step done with a real parser instead of
# regex/LLM guessing. Used to check that a generated patch keeps the file
# syntactically valid before it's ever shipped to a real build.

def _first_error(node, src: bytes, limit: int = 5) -> list[str]:
    """Return up to `limit` 'line N: <snippet>' for ERROR/MISSING nodes."""
    errs: list[str] = []
    stack = [node]
    while stack and len(errs) < limit:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            line = n.start_point[0] + 1
            snippet = src[n.start_byte:min(n.end_byte, n.start_byte + 60)].decode("utf-8", "ignore")
            errs.append(f"line {line}: {('MISSING ' if n.is_missing else 'ERROR ')}{snippet!r}")
        stack.extend(n.children)
    return errs


def parse_ok(text: str, suffix: str) -> tuple[bool, list[str]]:
    """(is_valid, problems). If the language has no grammar, returns (True, [])
    — we can't judge, so we don't block (a real build will catch the rest)."""
    lang = _AST_LANG.get(suffix.lower())
    parser = _get_parser(lang) if lang else None
    if parser is None:
        return True, []
    try:
        tree = parser.parse(bytes(text, "utf-8"))
    except Exception as e:
        return False, [f"parser failed: {e}"]
    errs = _first_error(tree.root_node, text.encode("utf-8"))
    return (len(errs) == 0), errs


def apply_unified_diff(original: str, diff_text: str) -> str | None:
    """Apply a unified diff to `original` in memory (no git, no disk).
    Returns the patched text, or None if a hunk's context doesn't match."""
    orig = original.splitlines()
    out: list[str] = []
    i = 0  # index into orig
    lines = diff_text.splitlines()
    k = 0
    saw_hunk = False
    while k < len(lines):
        ln = lines[k]
        if ln.startswith("@@"):
            saw_hunk = True
            m = re.search(r"@@ -(\d+)(?:,(\d+))? \+", ln)
            if not m:
                return None
            start = int(m.group(1)) - 1
            if start < 0:
                start = 0
            out.extend(orig[i:start])  # copy unchanged lines up to the hunk
            i = start
            k += 1
            while k < len(lines) and not lines[k].startswith("@@") \
                    and not lines[k].startswith("--- ") and not lines[k].startswith("+++ "):
                h = lines[k]
                if h.startswith("+"):
                    out.append(h[1:])
                elif h.startswith("-"):
                    if i >= len(orig) or orig[i].strip() != h[1:].strip():
                        return None  # context mismatch → can't apply
                    i += 1
                elif h.startswith(" ") or h == "":
                    ctx = h[1:] if h.startswith(" ") else h
                    if i < len(orig):
                        if ctx.strip() and orig[i].strip() != ctx.strip():
                            return None  # context line doesn't match the real file
                        out.append(orig[i]); i += 1
                    else:
                        out.append(ctx)
                k += 1
        else:
            k += 1
    if not saw_hunk:
        return None
    out.extend(orig[i:])  # trailing unchanged lines
    return "\n".join(out)


# VSS catalogs are hierarchical DATA, not code — chunk them by signal, one leaf
# per chunk, keyed by the full dotted path (Vehicle.Cabin.Seat.Row1.Position).
# Unwraps the "children" wrapper so paths are clean (the labelling bug the thesis
# hit). Non-VSS yaml/json falls back to the regex chunker.
_VSS_EXTS = {".yaml", ".yml", ".json", ".vss"}


def _looks_like_vss(data) -> bool:
    if not isinstance(data, dict):
        return False
    if "Vehicle" in data:
        return True
    blob = str(data)[:4000]
    return ('"children"' in blob or "'children'" in blob or "datatype" in blob)


def _vss_walk(node, prefix, out):
    if not isinstance(node, dict):
        return
    children = node.get("children") if isinstance(node.get("children"), dict) else None
    if "datatype" in node and node.get("type") != "branch":
        # a leaf signal — emit it
        keep = {k: node[k] for k in ("datatype", "type", "unit", "description",
                                     "min", "max", "allowed", "vhal_property",
                                     "property", "areaId") if k in node}
        out.append((prefix, keep))
    for name, child in (children or {}).items():
        _vss_walk(child, f"{prefix}.{name}" if prefix else name, out)
    # some catalogs nest without a "children" wrapper
    if children is None:
        for name, child in node.items():
            if isinstance(child, dict) and name not in ("datatype", "type", "unit",
                                                        "description", "min", "max",
                                                        "allowed"):
                _vss_walk(child, f"{prefix}.{name}" if prefix else name, out)


def _vss_chunks(path: Path, text: str) -> list[dict] | None:
    if path.suffix.lower() not in _VSS_EXTS:
        return None
    try:
        if path.suffix.lower() == ".json":
            import json as _json
            data = _json.loads(text)
        else:
            import yaml as _yaml
            data = _yaml.safe_load(text)
    except Exception:
        return None
    if not _looks_like_vss(data):
        return None
    leaves: list[tuple] = []
    root = data.get("Vehicle") if "Vehicle" in data else data
    _vss_walk(root, "Vehicle" if "Vehicle" in data else "", leaves)
    if not leaves:
        return None
    chunks: list[dict] = []
    for sigpath, attrs in leaves:
        body = f"VSS signal: {sigpath}\n" + "\n".join(f"{k}: {v}" for k, v in attrs.items())
        chunks.append(_make_chunk(path, body, len(chunks)))
    return chunks


def chunk_file(path: Path, chunk_size: int = 1200, overlap: int = 200) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if not text.strip():
        return []

    # 1. VSS signal-tree chunking for VSS-looking yaml/json
    vss = _vss_chunks(path, text)
    if vss:
        return vss

    # 2. AST first for code; regex fallback for everything else / on failure.
    ast = _ast_chunks(path, text, chunk_size, overlap)
    if ast:
        return ast

    # Prefer structural splits when possible
    spans: list[str] = []
    last = 0
    for m in _SPLIT_RE.finditer(text):
        if m.start() - last > 80:
            spans.append(text[last:m.start()])
            last = m.start()
    spans.append(text[last:])

    chunks: list[dict] = []
    buf = ""
    for span in spans:
        if len(buf) + len(span) <= chunk_size:
            buf += span
            continue
        if buf.strip():
            chunks.append(_make_chunk(path, buf, len(chunks)))
        # hard-wrap long span with real overlap
        start = 0
        while start < len(span):
            end = min(len(span), start + chunk_size)
            piece = span[start:end]
            chunks.append(_make_chunk(path, piece, len(chunks)))
            if end >= len(span):
                break
            # step back by `overlap`, but always make forward progress
            start = max(end - overlap, start + 1)
        buf = ""
    if buf.strip():
        chunks.append(_make_chunk(path, buf, len(chunks)))
    return chunks


def _make_chunk(path: Path, content: str, idx: int) -> dict:
    return {
        "id": f"{path}::{idx}",
        "path": str(path),
        "content": content.strip()[:8000],
        "ext": path.suffix.lower(),
        "layer": guess_layer(str(path)),
    }


def guess_layer(path: str) -> str:
    p = path.replace("\\", "/").lower()
    if is_hidl(p):
        return "hidl_legacy"
    if any(x in p for x in ("/vss", "signal", "covesa")):
        return "vss"
    if p.endswith(".aidl") or "/aidl" in p:
        return "aidl"
    if "hardware/interfaces/automotive" in p or "vehiclehal" in p or "/vhal" in p:
        return "vhal"
    if "packages/services/car" in p:
        return "carservice"
    if any(x in p for x in ("packages/apps", "carui", "hmi", "systemui")):
        return "hmi"
    if p.endswith((".cpp", ".cc", ".c", ".h", ".hpp")):
        return "native"
    if "vendor/" in p or "device/" in p:
        return "customer"
    return "other"
