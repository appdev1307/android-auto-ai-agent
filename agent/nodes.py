"""Agent nodes with tool-calling + hybrid RAG (option A)."""

from __future__ import annotations
import os
import re
import yaml
from pathlib import Path
from typing import Dict, Any, Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from agent.state import AgentState
from agent.tools_def import ALL_TOOLS, set_retriever, get_retriever
from retrieval.hybrid import HybridRetriever
from retrieval.store import Tenant
from retrieval.chunker import (apply_unified_diff, parse_ok, grammar_missing, guess_layer,
                               clang_format_check, symbol_window)
from agent.specialists import make_specialists_node, format_specialist_notes
from agent.diagnosis import (Diagnosis, collect_evidence, commit_diagnosis_record,
                             render_diagnosis)


def load_config() -> dict:
    p = Path(__file__).resolve().parents[1] / "data" / "config.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def load_text(rel: str) -> str:
    p = Path(__file__).resolve().parents[1] / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_hints() -> str:
    """Custom operator hints — no code edit needed to add them.

    Drop any `*.md` file into the `hints/` folder (path configurable via
    `prompt.hints_dir`), and/or list explicit files in `prompt.hint_files`.
    All are appended to the system prompt, sorted by filename so you can order
    them (e.g. 00-power.md, 10-vss.md). Restart the process to pick up changes.
    """
    root = Path(__file__).resolve().parents[1]
    prompt_cfg = CFG.get("prompt", {}) or {}
    parts: list[str] = []
    hints_dir = root / prompt_cfg.get("hints_dir", "hints")
    if hints_dir.is_dir():
        for f in sorted(hints_dir.glob("*.md")):
            parts.append(f.read_text(encoding="utf-8"))
    for rel in prompt_cfg.get("hint_files", []) or []:
        parts.append(load_text(rel))
    if not parts:
        return ""
    return "\n\n# Operator hints (custom)\n" + "\n\n".join(parts)


CFG = load_config()
MODEL = CFG.get("model", {})
API_BASE = MODEL.get("api_base") or os.environ.get("OPENAI_API_BASE")
API_KEY = os.environ.get("OPENAI_API_KEY", "dummy")

_kwargs = {
    "model": MODEL.get("name", "meta-llama/Llama-3.1-70B-Instruct"),
    "temperature": MODEL.get("temperature", 0.1),
    "api_key": API_KEY,
    # Generation budget. The config value was previously NOT passed through, so
    # the endpoint default applied — and on OpenAI-compatible backends like
    # Ollama that default is small, silently truncating a patch/UT/commit-JSON
    # mid-output. The thesis measured a single VHAL generation needing ~4302
    # tokens against a 4096 cap, so we read it from config with generous
    # headroom and no silent truncation.
    "max_tokens": int(MODEL.get("max_tokens", 8192)),
}
if API_BASE:
    _kwargs["base_url"] = API_BASE

llm = ChatOpenAI(**_kwargs)
llm_with_tools = llm.bind_tools(ALL_TOOLS)

SYSTEM = load_text("prompts/system.md") + "\n\n" + load_text("prompts/fewshot_localize.md")
SYSTEM += "\n\n" + load_text("skills/AGENTS.md")
SYSTEM += "\n\n" + load_text("skills/android_automotive.md")
SYSTEM += "\n\n" + load_text("skills/patch_and_ut.md")
SYSTEM += load_hints()   # custom hints from hints/*.md + config prompt.hint_files


# Build the retriever once per root and reuse it. Rebuilding on every graph
# run reloads the embedder + cross-encoder and re-fits BM25 from the pickle,
# which is seconds-to-minutes on a real tree and kills interactive demos.
_RETRIEVER_CACHE: Dict[str, HybridRetriever] = {}


def _get_retriever(root: str | None, tenant: dict | None) -> HybridRetriever:
    t = Tenant(**tenant) if tenant else None
    key = f"{root}::{t.slug if t else 'base-only'}"
    r = _RETRIEVER_CACHE.get(key)
    if r is None:
        r = HybridRetriever(aosp_root=root, tenant=t)
        _RETRIEVER_CACHE[key] = r
    return r


def init_retriever(state: AgentState) -> Dict[str, Any]:
    root = state.get("aosp_root") or os.environ.get("AOSP_ROOT")
    tenant = state.get("tenant")
    r = _get_retriever(root, tenant)
    set_retriever(r)
    who = Tenant(**tenant).slug if tenant else "base-only"
    return {"status": "retriever_ready",
            "messages": [AIMessage(content=f"Hybrid RAG ready. tenant={who} root={root}")]}


MAX_TOOL_ITERS = int(CFG.get("agent", {}).get("max_tool_iters", 8))


def agent_reason(state: AgentState) -> Dict[str, Any]:
    """LLM with tools — localize / explain using hybrid RAG tools."""
    # Pass the running conversation intact (task framing was seeded once in
    # main.run). Do NOT hand-slice the trail: a window that starts on a
    # ToolMessage whose parent AIMessage(tool_calls) got cut = an orphan
    # tool response, which the OpenAI-compatible endpoint rejects (400).
    messages = [SystemMessage(content=SYSTEM)] + list(state.get("messages", []))
    resp = llm_with_tools.invoke(messages)
    iters = int(state.get("iterations", 0)) + 1
    return {"messages": [resp], "status": "reasoning", "iterations": iters}


def should_continue(state: AgentState) -> Literal["tools", "commit"]:
    # Hard stop before LangGraph's recursion_limit turns into a crash.
    if int(state.get("iterations", 0)) >= MAX_TOOL_ITERS:
        return "commit"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "commit"


tool_node = ToolNode(ALL_TOOLS)


_COMMIT_PROMPT = """You have finished gathering evidence with tools. Commit the diagnosis
as STRICT JSON and nothing else — no prose, no markdown, no code fences.

Use ONLY file paths, symbols, and identifiers that appear VERBATIM in the tool
results above. Do not invent anything; unproven items will be dropped.

JSON schema:
{
  "file": "<the single best file to patch, exact path from a tool result>",
  "symbols": ["<method/callback/class central to the fix, seen in evidence>"],
  "property_ids": ["<e.g. PERF_VEHICLE_SPEED, seen in evidence>"],
  "root_cause": "<one sentence>",
  "candidates": [
    {"path": "<exact path from evidence>", "layer": "<your guess>", "why": "<short>"}
  ]
}
Output the JSON object only."""


def commit_diagnosis(state: AgentState) -> Dict[str, Any]:
    """Turn the ReAct trail into a COMMITTED, code-validated diagnosis record.

    The LLM proposes JSON; `commit_diagnosis_record` then keeps only what is
    grounded in the evidence the agent actually gathered (paths/symbols/consts),
    recomputes the layer from the path, and snaps git a//b/ prefixes. Everything
    downstream (specialists, finalize, patch, UT) binds to this record instead of
    re-deriving from prose."""
    msgs = list(state.get("messages", []))
    evidence = collect_evidence(msgs)
    resp = llm.invoke([SystemMessage(content=SYSTEM)] + msgs
                      + [HumanMessage(content=_COMMIT_PROMPT)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    dx = commit_diagnosis_record(raw, evidence, guess_layer)
    # structured evidence for specialists to reuse (no independent re-retrieval)
    ev_records = [{"path": p, "layer": guess_layer(p)} for p in evidence["paths"]]
    return {
        "diagnosis": dx,
        "evidence": ev_records,
        "status": "diagnosis_committed",
        # not committed / grounding problems => a human must look
        "needs_human_review": bool(state.get("needs_human_review"))
                              or (not dx.get("committed"))
                              or bool(dx.get("grounding_problems")),
    }




SENSITIVE = ("vhal", "vss", "selinux", "power", "aidl", "hardware/interfaces")


_PATH_RE = re.compile(r"(?:^|\s)((?:[\w.\-]+/){1,}[\w.\-]+\.\w+)")
_GIT_AB_PREFIX = re.compile(r"^[ab]/")

# Unified-diff header lines. Their `--- a/<path>` / `+++ b/<path>` and
# `diff --git a/<path> b/<path>` write paths with git's synthetic a//b/
# prefixes; mining them yields bogus `a/<path>` / `b/<path>` "candidates" that
# never resolve on disk and then get flagged as "possible hallucination"
# against the tree (false positive). Candidate files come from the ranked list,
# not from the diff, so we skip these lines entirely.
_DIFF_HEADER_PREFIXES = ("--- ", "+++ ", "diff --git", "index ", "@@")


def _extract_candidate_paths(text: str) -> list[str]:
    # Grab path-ish tokens the model listed as candidate files, skipping
    # unified-diff header lines and stripping any leftover git a//b/ prefix so a
    # diff never manufactures phantom candidate paths.
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(_DIFF_HEADER_PREFIXES):
            continue
        for m in _PATH_RE.findall(line):
            out.append(_GIT_AB_PREFIX.sub("", m))
    return out


def _norm(s: str) -> str:
    # compare ignoring leading/trailing whitespace (indent noise from the model)
    return s.strip()


_DIFF_CONT = ("diff --git", "index ", "--- ", "+++ ", "@@")


def _strip_model_diffs(text: str, protect_from: str | None = None) -> tuple[str, bool]:
    """Remove model-authored unified-diff blocks, replacing each with a prose note.

    The ONLY unified diff we trust in a final report is the one produced by
    `_grounded_patch_loop` — it was applied in-memory to the real full file and
    parse-checked. Every other diff is a first-pass draft the model wrote from
    ~1200-char chunks; its context lines are usually wrong and it must never be
    presented as an apply-ready patch. This strips those drafts to prose.

    `protect_from` is the exact grounded-patch marker string; text from that
    marker onward is left untouched so the grounded patch survives. When it is
    None (no grounded patch was produced), every diff in the text is stripped.

    Returns (new_text, stripped). new_text is byte-identical to the input when
    nothing was stripped.
    """
    if protect_from and protect_from in text:
        head, rest = text.split(protect_from, 1)
        tail = protect_from + rest
    else:
        head, tail = text, ""

    lines = head.splitlines()
    out: list[str] = []
    stripped = False
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].lstrip()
        # A real diff starts at `diff --git ...` or a `--- ` line immediately
        # followed by `+++ ` (this avoids eating a markdown `---` rule or a
        # `- bullet`, which never has a `+++ ` on the next line).
        is_start = s.startswith("diff --git") or (
            s.startswith("--- ") and i + 1 < n and lines[i + 1].lstrip().startswith("+++ ")
        )
        if not is_start:
            out.append(lines[i]); i += 1; continue
        # drop a fence we already emitted just above the diff
        if out and out[-1].strip().startswith("```"):
            out.pop()
        seen_hunk = False
        while i < n:
            t = lines[i].lstrip()
            if t.startswith(_DIFF_CONT):
                seen_hunk = seen_hunk or t.startswith("@@")
                i += 1; continue
            # inside a hunk, body lines are ' '/'+'/'-' prefixed (or blank)
            if seen_hunk and (lines[i][:1] in (" ", "+", "-") or lines[i] == ""):
                i += 1; continue
            break
        # swallow the closing fence if present
        if i < n and lines[i].strip().startswith("```"):
            i += 1
        out.append("_(draft diff removed — not grounded against the real file this "
                   "run; see root cause and unit-test ideas above)_")
        stripped = True

    if not stripped:
        return text, False
    return ("\n".join(out) + tail), True


def validate_diffs(text: str, read_file) -> list[str]:
    """Check every unified-diff hunk against the REAL file in the downloaded folder.

    No git needed — we only read files. For each hunk we take its "before" side
    (context ' ' + removed '-' lines) and confirm that exact sequence exists in
    the target file. A fabricated diff (invented line content) won't match.

    Returns a list of human-readable problems; empty means all diffs check out.
    """
    problems: list[str] = []
    cur_path = None
    before: list[str] = []          # before-side lines of the current hunk
    in_hunk = False

    def check(path, before_lines):
        if not path or not before_lines:
            return
        content = read_file(path, max_chars=200_000)
        if content.startswith("[error") or content.startswith("[refused"):
            problems.append(f"{path}: target file not found in the source folder")
            return
        file_lines = [_norm(l) for l in content.splitlines()]
        want = [_norm(l) for l in before_lines if _norm(l) != ""]
        if not want:
            return
        # find the first `want` line, then require the rest to follow in order
        joined = "\n".join(file_lines)
        block = "\n".join(want)
        if block not in joined:
            problems.append(
                f"{path}: hunk context does not match the real file "
                f"(diff may be fabricated / against a different version)")

    for line in text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            # new file target — flush previous hunk
            if in_hunk:
                check(cur_path, before); before = []; in_hunk = False
            m = re.search(r"[ab]/(\S+)", line) or re.search(r"\+\+\+ (\S+)", line)
            if m:
                cur_path = m.group(1)
            continue
        if line.startswith("@@"):
            if in_hunk:
                check(cur_path, before)
            before = []; in_hunk = True
            continue
        if in_hunk:
            if line.startswith(" ") or line.startswith("-"):
                before.append(line[1:])
            elif line.startswith("+"):
                pass  # added lines aren't in the original
            else:
                check(cur_path, before); before = []; in_hunk = False
    if in_hunk:
        check(cur_path, before)
    return problems


def _extract_first_diff(text: str) -> str:
    """Pull the unified-diff block out of an LLM answer (handles ``` fences)."""
    m = re.search(r"(--- [ab]?/?\S+.*?)(?:\n```|\Z)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text if "@@" in text else ""


# gtest/gmock/std/binder scaffolding + language keywords a test legitimately
# introduces — these are NOT "invented" symbols and must never be flagged.
_UT_SCAFFOLD = {
    "TEST", "TEST_F", "TEST_P", "EXPECT_CALL", "EXPECT_EQ", "EXPECT_NE", "EXPECT_TRUE",
    "EXPECT_FALSE", "EXPECT_THAT", "ASSERT_TRUE", "ASSERT_FALSE", "ASSERT_EQ", "ASSERT_NE",
    "INSTANTIATE_TEST_SUITE_P", "RUN_ALL_TESTS", "MOCK_METHOD", "SetUp", "TearDown",
    "TestWithParam", "ValuesIn", "GetParam", "PrintInstanceNameToString", "InitGoogleTest",
    "Times", "Return", "WillOnce", "override", "public", "protected", "private", "namespace",
    "nullptr", "void", "auto", "const", "using", "class", "struct", "include", "std", "make",
    "testing", "true", "false", "NULL", "TRUE", "FALSE", "ndk", "ScopedAStatus", "descriptor",
    "AServiceManager_waitForService", "ABinderProcess_startThreadPool", "fromBinder",
    "SpAIBinder", "super", "this", "return", "new", "else", "for", "while", "switch", "case",
    "break", "import", "package", "final", "static", "throws", "assertEquals", "assertTrue",
    "assertNotNull", "verify", "when", "mock", "RunWith", "Before", "After",
}


def _patch_added_text(patch_text: str) -> str:
    return "\n".join(l[1:] for l in patch_text.splitlines()
                     if l.startswith("+") and not l.startswith("+++"))


def _section(text: str, *header_needles: str) -> str:
    """Return the body of the first `## ...` section whose header contains any of
    the needles, up to the next `## ` header (or end)."""
    lines = text.splitlines()
    out, grabbing = [], False
    for ln in lines:
        if ln.lstrip().startswith("## "):
            if grabbing:
                break
            grabbing = any(n.lower() in ln.lower() for n in header_needles)
            continue
        if grabbing:
            out.append(ln)
    return "\n".join(out)


def ut_consistency(source_text: str, patch_text: str, ut_text: str) -> list[str]:
    """Check the generated unit test against the generated patch + real file.

    Build-free, heuristic. Two signals:
      (A) the test must reference at least one symbol the PATCH added/changed —
          otherwise it doesn't exercise the fix (patch/test inconsistent).
      (B) CONSTANT_CASE identifiers used in the test that appear in neither the
          file nor the patch — likely invented (e.g. a wrong property id).
    Test scaffolding (gtest/gmock/keywords) is excluded so it isn't mis-flagged.
    """
    if not ut_text.strip():
        return []
    tok = lambda s: set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", s))
    src, ut = tok(source_text), tok(ut_text)
    padd = tok(_patch_added_text(patch_text))
    grounded = src | padd
    problems: list[str] = []

    patch_sig = {t for t in padd if t not in _UT_SCAFFOLD and len(t) > 3}
    if patch_sig and not (patch_sig & ut):
        problems.append("unit test references NONE of the patched symbols ("
                        + ", ".join(sorted(patch_sig)[:6])
                        + ") — it likely does not exercise the fix (patch/test inconsistent)")

    ut_consts = {c for c in re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", ut_text)
                 if c not in _UT_SCAFFOLD}
    ungrounded = sorted(c for c in ut_consts if c not in grounded)
    if ungrounded:
        problems.append("unit test uses constant(s) not seen in the file or patch — "
                        "verify they are real identifiers: " + ", ".join(ungrounded[:6]))
    return problems


PATCH_MAX_TRIES = int(CFG.get("agent", {}).get("patch_max_tries", 2))


def _grounded_patch_loop(full: str, top: str, bug: str, summary: str,
                         truncated: bool = False) -> tuple[str, str]:
    """Generate a unified diff, apply it in-memory to the real file, parse-check
    the result with tree-sitter, and on any syntax/apply error feed the concrete
    problem back and regenerate (up to patch_max_tries). This is the C4
    generate→validate→regenerate loop with a REAL parser as the oracle instead
    of regex/LLM guessing.

    Returns (patch_text, note). note is '' when the patch applies cleanly and
    parses; otherwise it explains the remaining problem (and review is forced).
    """
    from pathlib import Path as _P
    suffix = _P(top).suffix
    # Marker shown to the MODEL only (so it knows the file is cut); it is NOT in
    # `full`, so it never reaches apply_unified_diff / parse_ok.
    trunc = "\n... [file truncated for context] ..." if truncated else ""
    last_err = None
    patch_text = "N/A"
    for _ in range(PATCH_MAX_TRIES + 1):
        instr = (f"Full current content of `{top}` below. Output ONLY a unified diff that "
                 f"applies CLEANLY against this exact file (real context lines, correct path), "
                 f"or `N/A` if no change is warranted.")
        if last_err:
            instr += f"\n\nYour previous diff was rejected: {last_err}\nProduce a corrected diff."
        msgs = [
            SystemMessage(content=SYSTEM),
            HumanMessage(content=f"Bug:\n{bug}\n\nRoot-cause summary:\n{summary}"),
            HumanMessage(content=f"{instr}\n\n```\n{full}{trunc}\n```"),
        ]
        resp = llm.invoke(msgs)
        patch_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        if "@@" not in patch_text:
            return patch_text, ""  # model decided no change (N/A)
        diff = _extract_first_diff(patch_text)
        patched = apply_unified_diff(full, diff)
        if patched is None:
            last_err = "the diff did not apply (a hunk's context did not match the file)."
            continue
        # A truncated file is syntactically incomplete, so parse_ok would always
        # false-fail. If the diff applied to the visible portion, accept it but
        # be honest that syntax wasn't verified.
        if truncated:
            return patch_text, ("Large file: patch was generated against the "
                                "symbol-anchored region (not the whole file), so syntax "
                                "wasn't fully verified and hunk offsets are region-relative "
                                "— apply with context matching and verify on a real build.")
        ok, errs = parse_ok(patched, suffix)
        if ok:
            # parse_ok returns ok=True both for "parsed cleanly" and for "no
            # grammar to parse with". Don't let the latter masquerade as a real
            # syntax pass on a C++/native (or Java/Kotlin) patch — say it plainly.
            if grammar_missing(suffix):
                return patch_text, (f"Syntax NOT verified: no tree-sitter grammar for "
                                    f"'{suffix}' is installed here, so the parse-check was "
                                    f"skipped. Install the grammar or verify on a real build.")
            # Convention gate (C/C++): the added lines must be clang-format clean
            # (AOSP style). On a violation, feed the concrete reformat back and
            # regenerate; if clang-format isn't installed, say style wasn't
            # enforced rather than pretending it passed.
            cf_status, cf_probs = clang_format_check(full, patched, suffix,
                                                     assume_filename=top)
            if cf_status == "violations":
                last_err = "the added C++ is not clang-format clean (AOSP style). " \
                           + " ".join(cf_probs[1:])
                continue
            if cf_status == "unavailable":
                return patch_text, ("Applies + parses, but C++ style NOT enforced: "
                                    + cf_probs[0] + " — run clang-format or verify on a real build.")
            return patch_text, ""  # clean: applies + parses + clang-format clean
        last_err = "applying it introduces a syntax error — " + "; ".join(errs[:3])
    return patch_text, ("Generated patch still fails validation (syntax/apply/style) "
                        "after retries: " + (last_err or "") + " Verify on a real build.")


_UT_PROMPT = """Finalize. No more tools. Follow skills/patch_and_ut.md.

The diagnosis is ALREADY COMMITTED (below). Do NOT restate candidates or root
cause, do NOT change the target file, and do NOT write a unified diff (the patch
is generated separately and grounded against the real file).

Produce ONLY this section:

## Unit test ideas
AAOS-native frameworks only (Java/HMI/CarService: JUnit4 + Robolectric or
instrumentation; VHAL/native: GoogleTest + gmock; HAL: VTS when applicable).
The test MUST exercise the committed symbols / property_ids below and reference
ONLY names that appear in them or in the target file — do not invent API.
For each test: Framework + TestName + setup/action/assert.
"""

ALWAYS_REVIEW_LAYERS = {"native", "vhal", "vss", "aidl", "hidl_legacy", "selinux"}


def finalize(state: AgentState) -> Dict[str, Any]:
    """Render the COMMITTED diagnosis and bind the patch + unit test to it.

    Candidates and root cause are no longer re-derived from prose — they are
    rendered from the committed `diagnosis` record. The LLM only writes the unit
    test (bound to the committed symbols); the patch comes from the grounded
    loop on the committed file. All oracles still run as a double-check.
    """
    dx = state.get("diagnosis") or {}
    r = get_retriever()
    needs_review = bool(state.get("needs_human_review"))

    # 1) The committed diagnosis IS the candidate list + root cause (source of truth).
    text = render_diagnosis(dx)
    spec_notes = format_specialist_notes(state.get("specialist_notes") or [])
    text += spec_notes

    # 2) Unit test — LLM writes it, bound to the committed facts only.
    facts = (f"Committed diagnosis:\n- file: {dx.get('file')}\n"
             f"- layer: {dx.get('layer')}\n"
             f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
             f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
             f"- root_cause: {dx.get('root_cause')}")
    try:
        resp = llm.invoke([SystemMessage(content=SYSTEM + spec_notes),
                           HumanMessage(content=facts),
                           HumanMessage(content=_UT_PROMPT)])
        ut_text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception:
        ut_text = "## Unit test ideas\n_(unit test generation unavailable)_"
    text += "\n\n" + ut_text

    # 3) Safety-critical layers ALWAYS require a human — from the committed layers.
    committed_layers = {dx.get("layer", "")} | {c.get("layer", "")
                                                for c in dx.get("candidates", []) or []}
    critical = committed_layers & ALWAYS_REVIEW_LAYERS
    if critical:
        needs_review = True
        text += "\n\n> ⚠ Human review required: touches safety-critical layer(s): " \
                + ", ".join(sorted(critical)) + "."

    # Power/SELinux safety net: some sensitive concerns aren't a clean file layer
    # (power management is spread across CarService + policy XML). Check the
    # COMMITTED facts only (file path + root cause), not the whole prose, so this
    # can't false-fire on an unrelated word elsewhere in the report.
    committed_text = (dx.get("file", "") + " " + (dx.get("root_cause") or "")).lower()
    if any(k in committed_text for k in ("selinux", "sepolicy", "power")):
        needs_review = True

    # 4) Patch — generated ONLY against the committed, grounded file.
    grounded_marker = None
    grounded_source = ""
    grounded_patch = ""
    target = dx.get("file", "")
    if r is not None and dx.get("committed") and target:
        if getattr(r, "source_present", True):
            budget = 24000
            # Read the whole file (big cap), then window on the committed symbols
            # so the model sees the code that actually changes even when it sits
            # past the first `budget` chars — instead of a blind first-N slice.
            full = r.read_file(target, max_chars=400000, add_marker=False)
            if not full.startswith("[error") and not full.startswith("[refused"):
                window, windowed = symbol_window(full, dx.get("symbols") or [], budget)
                patch_text, syntax_note = _grounded_patch_loop(
                    window, target, state.get("bug_report", ""),
                    dx.get("root_cause", ""), truncated=windowed)
                marker = "\n\n## Patch (grounded in full file: " + target + ")\n"
                if "@@" in patch_text:
                    text += marker + patch_text
                    grounded_marker = marker
                    grounded_source = window
                    grounded_patch = patch_text
                else:
                    text += marker + "N/A — no minimal change warranted after " \
                            "reading the full file."
                if syntax_note:
                    text += "\n\n> ⚠ " + syntax_note
                    needs_review = True
        else:
            text += "\n\n> ⚠ Patch not grounded: source folder not mounted " \
                    "(index-only mode) — verify manually."
            needs_review = True
    elif not dx.get("committed"):
        text += "\n\n> ⚠ Diagnosis is not grounded to a real file — no patch " \
                "generated; human review required."
        needs_review = True

    # 5) Strip any stray model-authored diff (e.g. if the UT LLM slipped one in);
    #    the grounded patch is protected.
    text, stripped_draft = _strip_model_diffs(text, protect_from=grounded_marker)
    if stripped_draft and grounded_marker is None:
        needs_review = True

    # 6) Diff grounding: every surviving hunk must match the real file.
    if r is not None and ("@@" in text or "--- " in text):
        diff_problems = validate_diffs(text, r.read_file)
        if diff_problems:
            text += "\n\n> ⚠ Diff did not validate against the source folder:\n>   - " \
                    + "\n>   - ".join(diff_problems) \
                    + "\n> Treat the patch as a described change, not an apply-ready diff."
            needs_review = True

    # 7) Cross-module consistency: does the unit test exercise the patch and avoid
    #    invented symbols? Checked against the committed source + grounded patch.
    if grounded_marker and grounded_patch:
        ut_body = _section(text, "unit test", "unit-test")
        # committed symbols/property_ids count as grounded even if defined elsewhere
        source_plus = grounded_source + "\n" + " ".join(
            (dx.get("symbols") or []) + (dx.get("property_ids") or []))
        ut_probs = ut_consistency(source_plus, grounded_patch, ut_body)
        if ut_probs:
            text += "\n\n> ⚠ Unit test not consistent with the patch:\n>   - " \
                    + "\n>   - ".join(ut_probs) \
                    + "\n> Align the test with the committed symbols before trusting it."
            needs_review = True

    return {
        "messages": [AIMessage(content=text)],
        "status": "completed",
        "needs_human_review": needs_review,
        "root_cause": dx.get("root_cause") or text[:2000],
        "candidate_files": [c["path"] for c in dx.get("candidates", []) or []],
    }


# Multi-agent per-layer specialists (built once, reuse shared llm + active retriever)
specialists = make_specialists_node(llm, get_retriever)