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


def chunk_file(path: Path, chunk_size: int = 1200, overlap: int = 200) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if not text.strip():
        return []

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
