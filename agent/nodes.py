"""Agent nodes with tool-calling + hybrid RAG (option A)."""

from __future__ import annotations
import os
import re
import yaml
from pathlib import Path
from typing import Dict, Any, Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.prebuilt import ToolNode

from agent.state import AgentState
from agent.tools_def import ALL_TOOLS, set_retriever, get_retriever
from agent.criteria import (PROMPT as CRITERIA_PROMPT, parse_criteria, validate_criteria,
                            render_criteria)
from retrieval.hybrid import HybridRetriever
from retrieval.store import Tenant
from retrieval.chunker import (apply_unified_diff, parse_ok, grammar_missing, guess_layer,
                               clang_format_check, symbol_window, read_enclosing_node)
from agent.specialists import make_specialists_node, format_specialist_notes
from agent.diagnosis import (collect_evidence, commit_diagnosis_record,
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


def load_skills() -> str:
    """Auto-load horizontal + global skill packs from skills/*.md.

    Sorted by filename (use 00-, 10-, … prefixes). CONTRACT.md is loaded first
    if present so every role shares the same rules. Explicit extras via
    `prompt.skill_files`. Restart to pick up changes. No code edit to add a pack.
    """
    root = Path(__file__).resolve().parents[1]
    prompt_cfg = CFG.get("prompt", {}) or {}
    skills_dir = root / prompt_cfg.get("skills_dir", "skills")
    parts: list[str] = []
    # Contract first (consistent rules among agents)
    for contract_name in ("CONTRACT.md", "00-CONTRACT.md"):
        cp = skills_dir / contract_name
        if cp.exists():
            parts.append(cp.read_text(encoding="utf-8"))
            break
    if skills_dir.is_dir():
        for f in sorted(skills_dir.glob("*.md")):
            if f.name.upper() in ("CONTRACT.MD", "00-CONTRACT.MD"):
                continue  # already first
            parts.append(f.read_text(encoding="utf-8"))
    for rel in prompt_cfg.get("skill_files", []) or []:
        parts.append(load_text(rel))
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


CFG = load_config()
MODEL = CFG.get("model", {})
# Env wins over config so `export OPENAI_API_BASE=...` (README step 4) or a Groq
# endpoint can be switched without editing data/config.yaml.
API_BASE = os.environ.get("OPENAI_API_BASE") or MODEL.get("api_base")
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

# system + fewshot + auto skills (CONTRACT + packs) + operator hints
SYSTEM = load_text("prompts/system.md") + "\n\n" + load_text("prompts/fewshot_localize.md")
SYSTEM += load_skills()
SYSTEM += load_hints()


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


def seed_retrieval(state: AgentState) -> Dict[str, Any]:
    """Deterministic first pass: run the hybrid retriever ONCE on the bug text
    before the ReAct loop, so localization never depends on the LLM choosing to
    call a search tool.

    Two outputs:
      - state['retrieved']: the structured ranked hits, used by commit as grounding
        substance and by finalize as the authoritative candidate list (fallback).
      - a HumanMessage listing the top real paths, so the agent grounds on indexed
        files instead of inventing a plausible-looking path.

    This also guarantees the full ranking stack (dense + BM25 + exact + reranker)
    actually runs every session.
    """
    r = get_retriever()
    bug = (state.get("bug_report") or "").strip()
    log = (state.get("logcat_snippet") or "").strip()
    query = (bug + "\n" + log).strip()
    hits: list = []
    if r is not None and query:
        try:
            hits = r.retrieve(query) or []
        except Exception:
            hits = []
    if not hits:
        return {"retrieved": []}

    lines = []
    for i, h in enumerate(hits[:8], 1):
        snippet = (h.get("content") or "")[:400].replace("\n", " ")
        lines.append(
            f"{i}. [{h.get('layer')}|{h.get('source')}|score={h.get('score', 0):.3f}] "
            f"{h.get('path')}\n   {snippet}"
        )
    seed_text = (
        "Seed retrieval — top hybrid-RAG hits for this bug. Use these EXACT paths "
        "as your candidates and target file; do not invent paths that are not "
        "listed here or returned by a tool:\n" + "\n".join(lines)
    )
    return {"retrieved": hits, "messages": [HumanMessage(content=seed_text)]}


_RELOC_TYPE = re.compile(r"\b[A-Z][A-Za-z0-9]+(?:Service|Manager|Hal|HAL|Policy|Controller|Callback|Property|Provider|Impl|Client)\b")
_RELOC_PATH = re.compile(r"\b[\w./-]+\.(?:java|kt|cpp|cc|h|aidl|vspec|json)\b")


def _relocalize_hints(notes: list, exclude_file: str = "",
                      extra_anchors: list | None = None) -> list[str]:
    """Mine concrete file/symbol names the DISAGREE-ing specialists pointed at
    (e.g. 'CarPropertyService', 'CarPowerManagementService'). Excludes the
    already-rejected target so re-seeding doesn't pull the same wrong file back.

    `extra_anchors` folds in the COMMITTED symbols / property_ids: the regexes
    below only catch CamelCase types, so an ALL_CAPS property id like
    PERF_VEHICLE_SPEED — which a specialist means when it says 'the implementation
    of PERF_VEHICLE_SPEED' — would otherwise yield zero hints and silently skip
    re-localization. These are real committed facts, not invented text.
    """
    excl_stem = ""
    if exclude_file:
        excl_stem = exclude_file.split("/")[-1].rsplit(".", 1)[0].lower()
    hints: list[str] = []
    for n in notes or []:
        if (n.get("verdict") or parse_verdict_safe(n)).upper() != "DISAGREE":
            continue
        txt = n.get("assessment") or ""
        hints += _RELOC_TYPE.findall(txt)
        hints += _RELOC_PATH.findall(txt)
    hints += [a for a in (extra_anchors or []) if a]
    seen: set[str] = set()
    out: list[str] = []
    for h in hints:
        hl = h.lower()
        if not h or hl in seen:
            continue
        if excl_stem and (hl == excl_stem or excl_stem in hl):
            continue
        seen.add(hl)
        out.append(h)
    return out


def parse_verdict_safe(note: dict) -> str:
    from agent.specialists import parse_verdict
    return note.get("verdict") or parse_verdict(note.get("assessment") or "")


def relocalize(state: AgentState) -> Dict[str, Any]:
    """Specialists strongly rejected the committed file but usually NAME the right
    artifacts. Mine those hints, run a targeted retrieval, and hand the agent a
    fresh candidate set so `commit` re-localizes instead of dead-ending at human
    review. Capped at one pass (see should_relocalize) to avoid loops."""
    r = get_retriever()
    dx = state.get("diagnosis") or {}
    notes = state.get("specialist_notes") or []
    bug = (state.get("bug_report") or "").strip()
    count = int(state.get("relocalize_count", 0)) + 1
    anchors = list(dx.get("property_ids") or []) + list(dx.get("symbols") or [])
    hints = _relocalize_hints(notes, exclude_file=dx.get("file", ""),
                              extra_anchors=anchors)
    if r is None or not hints:
        return {"relocalize_count": count, "status": "relocalize_empty"}
    # If a specialist says the fault is in the IMPLEMENTATION (interface file
    # rejected as the wrong layer), bias both the query and the ranking toward impl
    # source instead of another interface declaration.
    impl_cue = any("implement" in ((n.get("assessment") or "").lower()) for n in notes)
    q_extra = " implementation impl source" if impl_cue else ""
    query = (bug + " " + " ".join(hints[:6]) + q_extra).strip()
    try:
        hits = r.retrieve(query) or []
    except Exception:
        hits = []
    if not hits:
        return {"relocalize_count": count, "status": "relocalize_empty"}
    # Drop the rejected file, then (when impl is implicated) float implementation-
    # shaped paths above interface decls so `commit` re-localizes onto the code that
    # runs, not the .aidl it already rejected. Path shape (not layer) separates impl from
    # interface, so this also works for non-VHAL AIDL HALs.
    # Soft, stable re-rank: nothing is invented, hits are only reordered/filtered.
    excl_stem = (dx.get("file", "") or "").split("/")[-1].lower()

    def _is_impl(p: str) -> bool:
        pl = (p or "").lower()
        return pl.endswith((".cpp", ".cc", ".h")) or "/impl/" in pl or "fake_impl" in pl

    hits = [h for h in hits
            if (h.get("path") or "").split("/")[-1].lower() != excl_stem]
    if impl_cue:
        hits.sort(key=lambda h: not _is_impl(h.get("path") or ""))
    if not hits:
        return {"relocalize_count": count, "status": "relocalize_empty"}
    lines = []
    for i, h in enumerate(hits[:8], 1):
        snippet = (h.get("content") or "")[:300].replace("\n", " ")
        lines.append(f"{i}. [{h.get('layer')}|score={h.get('score', 0):.3f}] "
                     f"{h.get('path')}\n   {snippet}")
    msg = ("Re-localization — the specialists rejected the previous target "
           f"({dx.get('file')}) as the wrong file. Strongest layer hints they "
           f"named: {', '.join(hints[:6])}. Fresh top hybrid-RAG hits below — pick "
           "the correct target from THESE exact paths and re-commit:\n"
           + "\n".join(lines))
    return {"retrieved": hits, "relocalize_count": count,
            "status": "relocalized",
            # cleared so the re-commit + specialist pass decide review afresh
            "needs_human_review": False,
            "messages": [HumanMessage(content=msg)]}


def after_relocalize(state: AgentState) -> Literal["commit", "finalize"]:
    """Re-commit only when relocalize produced NEW candidates; otherwise another
    commit + specialist round would just re-judge the same rejected target."""
    return "commit" if state.get("status") == "relocalized" else "finalize"


def should_relocalize(state: AgentState) -> Literal["relocalize", "finalize"]:
    # One re-localization pass only.
    if int(state.get("relocalize_count", 0)) >= 1:
        return "finalize"
    c = state.get("specialist_consensus") or {}
    if not c.get("reject_committed"):
        return "finalize"
    dx = state.get("diagnosis") or {}
    anchors = list(dx.get("property_ids") or []) + list(dx.get("symbols") or [])
    if _relocalize_hints(state.get("specialist_notes") or [],
                         exclude_file=dx.get("file", ""), extra_anchors=anchors):
        return "relocalize"
    return "finalize"


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
    # Fold in the deterministic seed retrieval so grounding has real indexed
    # files to check against even when the LLM never called a search tool. The
    # seed paths + snippets become part of the evidence corpus, so a committed
    # file / symbol that matches an indexed hit now grounds instead of being
    # dropped wholesale.
    seeded = state.get("retrieved") or []
    if seeded:
        extra = "\n".join(
            f"{h.get('path', '')}\n{(h.get('content') or '')[:500]}"
            for h in seeded if h.get("path")
        )
        spaths = [h.get("path") for h in seeded if h.get("path")]
        evidence = {
            "corpus": (evidence["corpus"] + "\n" + extra).strip(),
            "paths": list(dict.fromkeys(list(evidence["paths"]) + spaths)),
        }
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




SENSITIVE = ("vhal", "vss", "selinux", "power", "aidl", "hardware/interfaces", "binder", "startup", "ignition", "resume", "frameworks")


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
                   "run; see root cause and unit test above)_")
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


def _offset_hunks(patch_text: str, offset: int) -> str:
    """Shift `@@ -a,b +c,d @@` by `offset` lines so a diff generated against a
    region (one AST node / symbol window) carries real file line numbers."""
    if offset <= 0:
        return patch_text

    def _sh(m):
        a, b, c, d, rest = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        out = f"@@ -{int(a) + offset}" + (f",{b}" if b is not None else "")
        out += f" +{int(c) + offset}" + (f",{d}" if d is not None else "")
        return out + " @@" + rest
    return re.sub(r"(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", _sh, patch_text)


def _grounded_patch_loop(full: str, top: str, bug: str, summary: str,
                         truncated: bool = False, fragment: bool = False) -> tuple[str, str]:
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
                                "wasn't fully verified — verify on a real build.")
        ok, errs = parse_ok(patched, suffix, fragment=fragment)
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


_UT_PROMPT = """Finalize. No more tools. Follow skills/20-patch_and_ut.md.

The diagnosis is ALREADY COMMITTED (below). Do NOT restate candidates or root
cause, do NOT change the target file, and do NOT write a unified diff (the patch
is generated separately and grounded against the real file).

Produce ONLY this section:

## Unit test
Write a COMPLETE, COMPILABLE test file — real code, NOT bullet-point ideas or a
sketch. It must fail before the fix and pass after it. Use the AAOS-native
framework for the layer:
  - Java (CarService/HMI): JUnit4 + Robolectric or AndroidJUnit4 — full class with
    package, imports, @RunWith, @Test methods and Mockito mocks.
  - C++ (VHAL/native): GoogleTest + gmock — full TEST_F with fixture and #includes.
  - HAL: VTS-style test when applicable.
Format: first a single line `Target test path: <dir>/<TestClass>.<ext>`, then the
FULL test inside one fenced code block (```java or ```cpp).
Bind the test to the COMMITTED symbols / property_ids below and reference ONLY
names that appear in them or in the target file excerpt — do NOT invent API,
class names, or methods. If a needed symbol is missing from the evidence, use the
closest committed symbol instead of inventing one.
If acceptance criteria [XK-n] are given above, each `unit` criterion must map to one
@Test / TEST_F whose name or comment cites its id (e.g. `// XK-2`). Do not add, drop or
reword a criterion; if one cannot be expressed as a unit test, leave it to QC.
"""

ALWAYS_REVIEW_LAYERS = {"native", "vhal", "vss", "aidl", "hidl_legacy", "selinux", "binder", "startup_power", "frameworks"}


def finalize(state: AgentState) -> Dict[str, Any]:
    """Render the COMMITTED diagnosis and bind the patch + unit test to it.

    Candidates and root cause are no longer re-derived from prose — they are
    rendered from the committed `diagnosis` record. The LLM only writes the unit
    test (bound to the committed symbols); the patch comes from the grounded
    loop on the committed file. All oracles still run as a double-check.
    """
    if state.get("workflow") == "requirement":
        return _finalize_requirement(state)
    dx = state.get("diagnosis") or {}
    crit = state.get("criteria") or []
    decision = state.get("user_decision") or ""
    if decision == "reject":
        # Human gate said no: render what was localized + the reason, write nothing.
        reason = state.get("user_notes") or "(no reason given)"
        text = (render_diagnosis(dx) + "\n\n" + render_criteria(crit, None)
                + "\n\n## Rejected at the human gate\n" + reason
                + "\n\nNo patch and no unit test were generated.")
        return {"messages": [AIMessage(content=text)], "status": "rejected_by_user",
                "needs_human_review": True,
                "root_cause": dx.get("root_cause") or "",
                "candidate_files": [c["path"] for c in dx.get("candidates", []) or []]}
    approved = True if decision == "approve" else False
    notes = state.get("user_notes") or ""
    crit_block = ""
    if crit:
        crit_block = "\n\nAcceptance criteria (fixed at the gate — do not change):\n" + \
            "\n".join(f"- [{c['id']}] ({c['kind']}) {c['check']}" for c in crit)
    if notes:
        crit_block += "\n\nUser constraints (verbatim):\n" + notes
    r = get_retriever()
    needs_review = bool(state.get("needs_human_review"))
    consensus = state.get("specialist_consensus") or {}
    if consensus.get("force_human_review"):
        needs_review = True

    # Owning specialists gave the committed target no weighted support and at least
    # one rejected it, and the (capped) re-localization never produced a target they
    # accept — so `reject_committed` is STILL set when we reach finalize. A successful
    # re-localization would have cleared it on the last specialists pass. Binding a
    # patch / unit test / rendered root-cause to a target the specialists refuted is
    # exactly what produced the misleading "N/A — no change" patch and a unit test
    # that validates the refuted hypothesis. When rejected we localize-and-flag for a
    # human instead of fabricating a fix against a dead target.
    rejected = bool(consensus.get("reject_committed"))
    if rejected:
        needs_review = True

    # 1) The committed diagnosis IS the candidate list + root cause (source of truth).
    #    Fallback: if the LLM grounded no candidate, show the retriever's own
    #    ranked hits so localization always yields real, indexed files instead of
    #    collapsing to "(none grounded)". The patch stays gated on `committed`, so
    #    this is localization-only — it never fabricates a patch.
    fallback_candidates = False
    if not dx.get("candidates"):
        fb = []
        for h in (state.get("retrieved") or [])[:8]:
            p = h.get("path")
            if not p:
                continue
            fb.append({"path": p, "layer": h.get("layer") or guess_layer(p),
                       "why": f"retriever-ranked (score={h.get('score', 0):.3f})"})
        if fb:
            dx = {**dx, "candidates": fb}
            fallback_candidates = True

    text = render_diagnosis(dx, rejected=rejected)
    if fallback_candidates:
        text += ("\n\n> \u2139 Candidates above are the retriever's top hybrid-RAG "
                 "hits (the LLM committed no grounded file). Localization only — "
                 "verify before use.")
    spec_notes = format_specialist_notes(
        state.get("specialist_notes") or [], consensus=consensus
    )
    text += spec_notes
    if not rejected:  # nothing to accept on a refuted target
        text += "\n\n" + render_criteria(crit, approved)

    if rejected:
        # The rendered root cause above is the REJECTED committed hypothesis — say so
        # loudly so it is not read as the answer, and hand the human the leads the
        # specialists named plus the retriever's re-localization candidates. No patch
        # and no unit test are generated against a refuted target (sections 2 & 4 are
        # skipped below).
        rc = dx.get("root_cause") or "(none)"
        hints = _relocalize_hints(state.get("specialist_notes") or [],
                                  exclude_file=dx.get("file", ""))
        text += ("\n\n## ⚠ Localization unresolved — committed target rejected\n"
                 f"Owning specialists gave no weighted support to `{dx.get('file')}` "
                 "and re-localization did not converge on a target they accept, so no "
                 "patch or unit test was generated.\n"
                 f"- Rejected root-cause hypothesis: {rc}\n"
                 f"- Specialist leads to investigate next: "
                 f"{', '.join(hints[:8]) or '(none named)'}\n")
        leads = []
        rejected_file = dx.get("file") or ""
        for h in (state.get("retrieved") or [])[:8]:
            p = h.get("path")
            if p and p != rejected_file:
                leads.append(f"  - [{h.get('layer') or guess_layer(p)}] {p}")
        if leads:
            text += "- Retriever re-localization candidates:\n" + "\n".join(leads) + "\n"

    # 2) Unit test — LLM writes REAL compilable test code, bound to committed facts
    #    and the target file's actual API (fed below so it doesn't invent methods).
    #    Skipped on a rejected target: a test bound to the refuted symbols only
    #    validates the wrong hypothesis (e.g. asserting PERF_VEHICLE_SPEED == FLOAT
    #    when the AIDL already declares FLOAT).
    if not rejected:
        ut_file = dx.get("file") or ""
        ut_excerpt = ""
        if r is not None and ut_file:
            try:
                ut_excerpt = r.read_file(ut_file, max_chars=4000)
            except Exception:
                ut_excerpt = ""
        facts = (f"Committed diagnosis:\n- file: {dx.get('file')}\n"
                 f"- layer: {dx.get('layer')}\n"
                 f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
                 f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
                 f"- root_cause: {dx.get('root_cause')}{crit_block}\n\n"
                 f"Target file excerpt (reference REAL API from here — do not invent):\n"
                 f"{ut_excerpt or '(not available)'}")
        try:
            resp = llm.invoke([SystemMessage(content=SYSTEM + spec_notes),
                               HumanMessage(content=facts),
                               HumanMessage(content=_UT_PROMPT)])
            ut_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        except Exception:
            ut_text = "## Unit test\n_(unit test generation unavailable)_"
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

    # 4) Patch — generated ONLY against the committed, grounded file, and ONLY when
    #    the specialists did not reject that file. On a rejected target the file is
    #    (correctly) fine, so the loop returns "no change" — but printing "N/A — no
    #    minimal change warranted" reads as "the file is clean and this is the answer",
    #    hiding that the target was refuted. Say the real reason instead.
    grounded_marker = None
    grounded_source = ""
    grounded_patch = ""
    target = dx.get("file", "")
    if rejected:
        text += ("\n\n## Patch\nNo patch generated — the committed target was rejected "
                 "by the owning specialists (see 'Localization unresolved' above). "
                 "Re-localize to an accepted target before patching.")
    elif r is not None and dx.get("committed") and target:
        if getattr(r, "source_present", True):
            budget = 24000
            # Read the whole file (big cap), then window on the committed symbols
            # so the model sees the code that actually changes even when it sits
            # past the first `budget` chars — instead of a blind first-N slice.
            full = r.read_file(target, max_chars=400000, add_marker=False)
            if not full.startswith("[error") and not full.startswith("[refused"):
                # Prefer a COMPLETE enclosing AST node over a char-truncated window:
                # a whole function/class is syntactically self-contained, so the patch
                # oracle can actually run parse_ok on it (truncated=False) instead of
                # skipping the check as it must for a mid-file char cut. Fall back to
                # the char window when no node fits (big node, .aidl/no grammar, etc.).
                node = None
                for _sym in (dx.get("symbols") or []) + (dx.get("property_ids") or []):
                    node = read_enclosing_node(target, _sym, text=full, max_chars=budget)
                    if node:
                        break
                if node:
                    window, windowed = node["code"], False
                    region = f"{target}:{node['start_line']}-{node['end_line']}"
                    offset = node["start_line"] - 1
                else:
                    window, windowed = symbol_window(full, dx.get("symbols") or [], budget)
                    region = target
                    pos = full.find(window) if windowed else 0
                    offset = full[:pos].count("\n") if pos > 0 else 0
                patch_text, syntax_note = _grounded_patch_loop(
                    window, target, state.get("bug_report", ""),
                    (dx.get("root_cause", "") or "") + crit_block, truncated=windowed,
                    fragment=bool(node))
                # diff was made against the region -> report real file line numbers
                patch_text = _offset_hunks(patch_text, offset)
                marker = "\n\n## Patch (grounded in " + region + ")\n"
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


def propose_criteria(state: AgentState) -> Dict[str, Any]:
    """Propose 2-5 acceptance criteria [XK-n] for the committed diagnosis.

    Runs once, after the specialists settle and before the (optionally gated)
    finalize. Output is validated deterministically (agent/criteria.py): vague
    items dropped, ids renumbered, identifiers not seen in the run flagged. No
    fallback invention — an empty list means the user gives them at the gate.
    Skipped when the target was rejected (nothing to accept) or criteria already
    exist (e.g. supplied by the user on --resume).
    """
    if state.get("workflow") == "requirement" or state.get("criteria"):
        return {}
    dx = state.get("diagnosis") or {}
    if not dx.get("file") or (state.get("specialist_consensus") or {}).get("reject_committed"):
        return {"criteria": []}
    ev = collect_evidence(state.get("messages") or [])
    r = get_retriever()
    excerpt = ""
    if r is not None:
        try:
            excerpt = r.read_file(dx["file"], max_chars=4000)
        except Exception:
            excerpt = ""
    facts = (f"Bug:\n{state.get('bug_report', '')}\n\n"
             f"Committed diagnosis:\n- file: {dx.get('file')}\n- layer: {dx.get('layer')}\n"
             f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
             f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
             f"- root_cause: {dx.get('root_cause')}\n\n"
             f"Target file excerpt:\n{excerpt or '(not available)'}")
    try:
        resp = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=facts),
                           HumanMessage(content=CRITERIA_PROMPT)])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception:
        raw = ""
    grounding = "\n".join([ev.get("corpus") or "", excerpt, state.get("bug_report") or "",
                           state.get("logcat_snippet") or "",
                           " ".join((dx.get("symbols") or []) + (dx.get("property_ids") or []))])
    return {"criteria": validate_criteria(parse_criteria(raw), grounding)}


# Multi-agent per-layer specialists (built once, reuse shared llm + active retriever)
specialists = make_specialists_node(llm, get_retriever)


# =====================================================================================
# requirement -> code workflow (multi-workflow). Bug path above is untouched.
# Deterministic backbone: route_workflow, decompose, consistency_check, regenerate.
# LLM-backed (grounded, fail-safe): extract_spec, generate_subtask.
# =====================================================================================
import json  # noqa: E402

MAX_REGEN = int(CFG.get("agent", {}).get("regen_cap", 1))
_SAFETY_LAYERS = {"aidl", "vhal", "carservice", "selinux", "binder"}

_REQ_HINTS = re.compile(
    r"\bas an?\b.{0,40}\bi want\b|acceptance criteria|user stor|epic\b|"
    r"the system (shall|should|must)|\bshall\b|high[- ]level requirement|\bHLR\b",
    re.I | re.S)
_BUG_HINTS = re.compile(
    r"logcat|stack ?trace|back ?trace|\bNPE\b|crash|\bANR\b|exception|fatal|"
    r"reproduc|reads?\s+0|returns?\s+0|shows?\s+0", re.I)


def _json_obj(text: str) -> dict:
    """Best-effort JSON object from an LLM reply (strips fences, takes first {...})."""
    if not text:
        return {}
    t = re.sub(r"```(?:json)?|```", "", text).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


# Fixed VHAL decomposition template. The skeleton is CODE, not a prompt: the LLM only
# fills each artifact body, so the plan stays deterministic and reviewable. `applies`
# gates optional layers off the spec. Targets are AAOS defaults; a real vendor HAL or
# a retriever hit can override them.
_VHAL_TEMPLATE = [
    dict(id="vss", layer="vss", deps=[],
         target="(VSS signal lookup)",
         intent="Confirm the VSS signal backing {feature}",
         applies=lambda s: bool(s.get("vss_signal"))),
    dict(id="aidl", layer="aidl", deps=["vss"],
         target="hardware/interfaces/automotive/vehicle/aidl_property/android/hardware/"
                "automotive/vehicle/VehicleProperty.aidl",
         intent="Declare {property_id} composed as area {area} | type {type} | SYSTEM",
         applies=lambda s: True),
    dict(id="config", layer="config", deps=["aidl"],
         target="hardware/interfaces/automotive/vehicle/aidl/impl/default_config/config/"
                "DefaultProperties.json",
         intent="Seed defaultValue for {property_id}; include sample rates if CONTINUOUS",
         applies=lambda s: True),
    dict(id="impl", layer="vhal", deps=["aidl", "config"],
         target="hardware/interfaces/automotive/vehicle/aidl/impl/fake_impl/hardware/src/"
                "FakeVehicleHardware.cpp",
         intent="Register and populate/update {property_id} of type {type}",
         applies=lambda s: True),
    dict(id="carservice", layer="carservice", deps=["impl"],
         target="packages/services/Car/service/src/com/android/car/CarPropertyService.java",
         intent="Forward {property_id} to clients with permission mapping",
         applies=lambda s: bool(s.get("user_facing"))),
    # binder: only when a NEW vendor service is introduced; an added property on an
    # existing service rides the already-registered binder interface (no change).
    dict(id="binder", layer="binder", deps=["impl"],
         target="hardware/interfaces/automotive/vehicle/aidl/impl/vhal/src/VehicleService.cpp",
         intent="Register vendor service {service_name} with ServiceManager; AIDL Bn/Bp for {property_id}",
         applies=lambda s: bool(s.get("new_service"))),
    # selinux: only when a new type/service label or vendor-writable path is added;
    # a property on the existing hal_vehicle domain needs no new .te.
    dict(id="selinux", layer="selinux", deps=["impl"],
         target="device/generic/car/common/sepolicy/vendor/hal_vehicle_vss.te",
         intent="Allow-only .te fragment scoped to {service_name}; grant property/data_file access for {property_id}",
         applies=lambda s: bool(s.get("needs_sepolicy") or s.get("new_service"))),
    # hmi: only for a user-facing feature that an app surfaces.
    dict(id="hmi", layer="hmi", deps=["carservice"],
         target="packages/apps/Car/CarSettings/src/com/android/car/settings/vss/"
                "{property_id}Client.java",
         intent="CarPropertyManager client subscribing/reading {property_id} for the UI",
         applies=lambda s: bool(s.get("user_facing"))),
    dict(id="test", layer="test", deps=["impl"],
         target="hardware/interfaces/automotive/vehicle/aidl_property/tests/"
                "VehiclePropertyTest.java",
         intent="Fail-to-pass test asserting {property_id} type/behaviour",
         applies=lambda s: True),
]

_XREF_KEYS = ("property_id", "type", "area", "change_mode", "vss_signal",
              "service_name", "permission")


def route_workflow(state: AgentState) -> Dict[str, Any]:
    """Classify the input: requirement (story/epic/HLR) vs bug (symptom/logcat).
    Deterministic heuristic; an explicit `requirement` field wins. No LLM needed."""
    req = (state.get("requirement") or "").strip()
    bug = (state.get("bug_report") or "").strip()
    text = req or bug
    if req and not bug:
        wf = "requirement"
    else:
        req_hit = bool(_REQ_HINTS.search(text))
        bug_hit = bool(_BUG_HINTS.search(text))
        wf = "requirement" if (req_hit and not bug_hit) else "bug"
    return {"workflow": wf}


def pick_workflow(state: AgentState) -> Literal["bug", "requirement"]:
    return "requirement" if state.get("workflow") == "requirement" else "bug"


def extract_spec(state: AgentState) -> Dict[str, Any]:
    """Requirement prose -> structured spec (the xref anchors), grounded on the
    retriever so property/type/VSS names are real. Fail-safe: on any error it emits
    an empty spec and flags human review rather than crashing the graph."""
    req = (state.get("requirement") or state.get("bug_report") or "").strip()
    r = get_retriever()
    ctx = ""
    try:
        if r is not None:
            hits = r.retrieve(req)[:5] or []
            ctx = "\n".join(f"- {h.get('path')}: {(h.get('content') or '')[:200]}"
                            for h in hits)
    except Exception:
        ctx = ""
    prompt = (
        "Extract a VHAL property spec from the requirement. Return ONLY JSON with keys: "
        "feature, property_id (UPPER_SNAKE), type (FLOAT|INT32|INT64|BOOLEAN|STRING), "
        "area (GLOBAL|WHEEL|DOOR|SEAT|WINDOW|MIRROR), "
        "change_mode (STATIC|ON_CHANGE|CONTINUOUS), vss_signal (dotted or null), "
        "user_facing (bool), permission (string or null), "
        "service_name (vendor VHAL service e.g. vendor.vss.adas, or null), "
        "new_service (bool: true only if a NEW vendor service must be registered), "
        "needs_sepolicy (bool: true only if a new SELinux label/type or vendor-writable "
        "path is introduced).\n\n"
        f"Requirement:\n{req}\n\nGrounding (real files):\n{ctx}")
    spec: dict = {}
    try:
        resp = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        spec = _json_obj(resp.content if isinstance(resp.content, str) else str(resp.content))
    except Exception:
        spec = {}
    needs_review = not bool(spec.get("property_id") and spec.get("type"))
    return {"spec": spec, "needs_human_review": needs_review or bool(state.get("needs_human_review"))}


def decompose(state: AgentState) -> Dict[str, Any]:
    """Instantiate the fixed VHAL template from the spec -> ordered subtasks. Every
    subtask carries a COPY of the spec's xref anchors, so consistency is later a
    structural equality, not an LLM opinion. Fully deterministic."""
    spec = state.get("spec") or {}
    xref = {k: spec.get(k) for k in _XREF_KEYS}
    subtasks = []
    fmt = {k: spec.get(k, "?") for k in
           ("feature", "property_id", "type", "area", "service_name", "permission")}
    for t in _VHAL_TEMPLATE:
        try:
            if not t["applies"](spec):
                continue
        except Exception:
            continue
        try:
            tgt = t["target"].format(**fmt)   # some targets embed {property_id}
        except Exception:
            tgt = t["target"]
        subtasks.append({
            "id": t["id"], "layer": t["layer"], "target": tgt,
            "intent": t["intent"].format(**fmt),
            "depends_on": list(t["deps"]), "xref": dict(xref),
            "artifact": "", "status": "pending",
        })
    return {"subtasks": subtasks, "regen_count": int(state.get("regen_count", 0))}


def _settled(subtasks: list, ids: list) -> bool:
    by = {s["id"]: s for s in subtasks}
    return all(by.get(i, {}).get("status") in ("done", "failed") for i in ids)


def generate_subtask(state: AgentState) -> Dict[str, Any]:
    """Generate ONE pending subtask whose deps are settled, grounded on the retriever
    + its xref + a real exemplar (read_enclosing_node). Marks done/failed and returns
    the FULL subtask list (no reducer). Sequential; a fan-out swaps only this node."""
    subtasks = [dict(s) for s in (state.get("subtasks") or [])]
    nxt = next((s for s in subtasks
                if s["status"] == "pending" and _settled(subtasks, s["depends_on"])), None)
    if nxt is None:
        return {"subtasks": subtasks}

    # VSS lookup is a confirmation step, not code generation.
    if nxt["layer"] == "vss":
        nxt["artifact"] = str(nxt["xref"].get("vss_signal") or "(none)")
        nxt["status"] = "done"
        return {"subtasks": subtasks}

    r = get_retriever()
    ctx, exemplar = "", ""
    try:
        if r is not None:
            pid = nxt["xref"].get("property_id") or ""
            hits = (r.retrieve(f"{pid} {nxt['layer']}") or [])[:4]
            ctx = "\n".join(f"- {h.get('path')}: {(h.get('content') or '')[:180]}" for h in hits)
            if pid:
                node = read_enclosing_node(nxt["target"], pid,
                                           root=getattr(r, "aosp_root", None)) or None
                if node:
                    exemplar = node["code"][:1500]
    except Exception:
        ctx, exemplar = "", ""

    prompt = (
        f"Produce the code change for this subtask. Target: {nxt['target']}\n"
        f"Intent: {nxt['intent']}\n"
        f"Shared anchors (MUST match exactly): {json.dumps(nxt['xref'])}\n"
        f"Output a unified diff if editing an existing file, else the full new file body. "
        f"No prose.\n\nGrounding:\n{ctx}\n\nExemplar of this layer:\n{exemplar}")
    try:
        resp = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        art = resp.content if isinstance(resp.content, str) else str(resp.content)
        nxt["artifact"] = art.strip()
        nxt["status"] = "done" if nxt["artifact"] else "failed"
    except Exception:
        nxt["status"] = "failed"
    return {"subtasks": subtasks}


def pick_gen(state: AgentState) -> Literal["generate", "check"]:
    subtasks = state.get("subtasks") or []
    has_pending = any(s["status"] == "pending" and _settled(subtasks, s["depends_on"])
                      for s in subtasks)
    return "generate" if has_pending else "check"


def consistency_check(state: AgentState) -> Dict[str, Any]:
    """Structural cross-layer verifier (deterministic). Checks the generated artifacts
    agree on the shared anchors — the 'reads 0' bug class (type/seed mismatch across
    layers) caught before it ships. Records offending subtask ids so regenerate can
    reset exactly those. (An LLM specialist verifier can be layered on top later.)"""
    spec = state.get("spec") or {}
    pid = (spec.get("property_id") or "").strip()
    ctype = (spec.get("type") or "").strip().upper()
    cmode = (spec.get("change_mode") or "").strip().upper()
    svc = (spec.get("service_name") or "").strip()
    conflicts: list = []
    done = [s for s in (state.get("subtasks") or []) if s["status"] == "done" and s["artifact"]]

    def _mentions(txt, tok):
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])", txt))

    for s in done:
        art = s["artifact"]
        # property-id carriers: the property name must appear verbatim
        if s["layer"] in ("aidl", "config", "vhal", "carservice", "hmi") and pid and \
                not _mentions(art, pid):
            conflicts.append({"id": s["id"], "reason": f"{s['id']} artifact omits {pid}"})
        # service-label carriers: the vendor service name must appear (selinux/binder)
        if s["layer"] in ("selinux", "binder") and svc and not _mentions(art, svc):
            conflicts.append({"id": s["id"], "reason": f"{s['id']} artifact omits service {svc}"})
        # a conflicting VehiclePropertyType token in aidl/impl (e.g. INT32 vs declared FLOAT)
        if s["layer"] in ("aidl", "vhal") and ctype:
            others = {"FLOAT", "INT32", "INT64", "BOOLEAN", "STRING"} - {ctype}
            hit = [o for o in others
                   if re.search(r"VehiclePropertyType\s*[:.]{1,2}\s*" + o, art)]
            if hit:
                conflicts.append({"id": s["id"],
                                  "reason": f"{s['id']} uses type {hit[0]}, spec says {ctype}"})
    # CONTINUOUS properties must seed sample rates in config
    if cmode == "CONTINUOUS":
        cfg = next((s for s in done if s["layer"] == "config"), None)
        if cfg and "ampleRate" not in cfg["artifact"]:
            conflicts.append({"id": "config", "reason": "CONTINUOUS but no sample rate seeded"})

    return {"req_consensus": {"consistent": not conflicts, "conflicts": conflicts}}


def after_check(state: AgentState) -> Literal["regenerate", "assemble"]:
    rc = state.get("req_consensus") or {}
    if rc.get("conflicts") and int(state.get("regen_count", 0)) < MAX_REGEN:
        return "regenerate"
    return "assemble"


def regenerate(state: AgentState) -> Dict[str, Any]:
    """Reset exactly the conflicting subtasks to pending, feed the reason back via
    intent, bump the cap. Analogous to relocalize for the bug path."""
    conflicts = (state.get("req_consensus") or {}).get("conflicts") or []
    bad = {c["id"]: c["reason"] for c in conflicts}
    subtasks = []
    for s in (state.get("subtasks") or []):
        s = dict(s)
        if s["id"] in bad:
            s["status"] = "pending"
            s["intent"] = f"{s['intent']}  [FIX: {bad[s['id']]}]"
            s["artifact"] = ""
        subtasks.append(s)
    return {"subtasks": subtasks, "regen_count": int(state.get("regen_count", 0)) + 1}


def assemble(state: AgentState) -> Dict[str, Any]:
    """Collect done artifacts into a change-set + traceability, hand off to finalize."""
    done = [s for s in (state.get("subtasks") or []) if s["status"] == "done" and s["artifact"]]
    return {"candidate_files": [s["target"] for s in done if s["layer"] != "vss"],
            "status": "assembled"}


def _finalize_requirement(state: AgentState) -> Dict[str, Any]:
    """Render the requirement->code change-set + traceability, with the same
    safety-critical human-review gate the bug path uses."""
    spec = state.get("spec") or {}
    subtasks = state.get("subtasks") or []
    rc = state.get("req_consensus") or {}
    feature = spec.get("feature") or spec.get("property_id") or "(requirement)"

    lines = [f"# Requirement → code: {feature}", ""]
    lines.append("## Spec")
    lines.append("```json\n" + json.dumps({k: spec.get(k) for k in
                 ("property_id", "type", "area", "change_mode", "vss_signal", "user_facing")},
                 indent=2) + "\n```")
    if not rc.get("consistent", True):
        lines.append("\n## ⚠ Cross-layer conflicts (regenerate did not fully resolve)")
        for c in rc.get("conflicts", []):
            lines.append(f"- **{c['id']}**: {c['reason']}")
    lines.append("\n## Change-set (traceability: requirement → artifact)")
    touched_safety = False
    for s in subtasks:
        state_tag = s["status"].upper()
        if s["layer"] in _SAFETY_LAYERS and s["status"] == "done":
            touched_safety = True
        lines.append(f"\n### [{s['layer']}] {s['target']}  ({state_tag})")
        if s["status"] == "done" and s["artifact"]:
            lines.append("```\n" + s["artifact"][:6000] + "\n```")
        elif s["layer"] == "vss":
            lines.append(f"VSS signal: `{s['artifact']}`")
        else:
            lines.append("_(not generated)_")

    needs_review = bool(state.get("needs_human_review")) or touched_safety \
        or not rc.get("consistent", True) \
        or any(s["status"] == "failed" for s in subtasks)
    if touched_safety:
        lines.append("\n> ⚠ Human review required: touches safety-critical layer(s).")

    return {
        "messages": [AIMessage(content="\n".join(lines))],
        "status": "completed",
        "needs_human_review": needs_review,
        "candidate_files": [s["target"] for s in subtasks if s["layer"] != "vss"],
    }