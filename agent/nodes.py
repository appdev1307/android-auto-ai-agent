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
from agent.tools_def import ALL_TOOLS, set_retriever
from retrieval.hybrid import HybridRetriever
from retrieval.store import Tenant


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
}
if API_BASE:
    _kwargs["base_url"] = API_BASE

llm = ChatOpenAI(**_kwargs)
llm_with_tools = llm.bind_tools(ALL_TOOLS)

SYSTEM = load_text("prompts/system.md") + "\n\n" + load_text("prompts/fewshot_localize.md")
SYSTEM += "\n\n" + load_text("skills/AGENTS.md")
SYSTEM += "\n\n" + load_text("skills/android_automotive.md")
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


def should_continue(state: AgentState) -> Literal["tools", "finalize"]:
    # Hard stop before LangGraph's recursion_limit turns into a crash.
    if int(state.get("iterations", 0)) >= MAX_TOOL_ITERS:
        return "finalize"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finalize"


tool_node = ToolNode(ALL_TOOLS)


SENSITIVE = ("vhal", "vss", "selinux", "power", "aidl", "hardware/interfaces")


def _extract_candidate_paths(text: str) -> list[str]:
    # Grab path-ish tokens the model listed as candidate files.
    return re.findall(r"(?:^|\s)((?:[\w.\-]+/){1,}[\w.\-]+\.\w+)", text)


def _norm(s: str) -> str:
    # compare ignoring leading/trailing whitespace (indent noise from the model)
    return s.strip()


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


def finalize(state: AgentState) -> Dict[str, Any]:
    """Ask model for final structured summary without new tools."""
    summary_prompt = HumanMessage(content="""Finalize now. No more tools.
Provide:
## Candidate files (ranked)   — one per line as `N. <full/path> [layer]`, only files seen in tool results
## Root cause                 — grounded in retrieved snippets
## Proposed patch (draft)     — unified diff if a code fix is warranted, else N/A
                                (a full-file-grounded diff is generated automatically after this)
## Unit test ideas
## needs_human_review: true/false
""")
    messages = [SystemMessage(content=SYSTEM)] + list(state.get("messages", [])) + [summary_prompt]
    resp = llm.invoke(messages)
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    low = text.lower()

    # Model self-report, but never trust it downward on safety-critical layers.
    model_says_ok = "needs_human_review: false" in low.replace("*", "").replace("`", "")
    touches_sensitive = any(s in low for s in SENSITIVE)
    needs_review = bool(touches_sensitive) or (not model_says_ok)

    # Path grounding: flag any candidate file that doesn't exist in the tree.
    retriever = set(_RETRIEVER_CACHE.values())
    r = next(iter(retriever), None)
    verified, unverified = [], []
    for p in dict.fromkeys(_extract_candidate_paths(text)):
        exists = False
        if r is not None:
            probe = r.read_file(p, max_chars=1)
            exists = not probe.startswith("[error") and not probe.startswith("[refused")
        (verified if exists else unverified).append(p)

    if unverified:
        text += "\n\n> ⚠ Unverified paths (not found in tree, possible hallucination): " \
                + ", ".join(unverified)

    # --- #1 Full-file context: regenerate the diff against the REAL full file ---
    # The first pass drafts a diff from ~1200-char chunks, so its context lines
    # are often wrong. If it proposed a diff, feed the top candidate's FULL
    # content and ask for a diff that applies cleanly against it. Bounded to one
    # file / ~24k chars so it fits the model's context window.
    if r is not None and "@@" in text and verified:
        if getattr(r, "source_present", True):
            top = verified[0]
            full = r.read_file(top, max_chars=24000)
            if not full.startswith("[error") and not full.startswith("[refused"):
                trunc = "\n... [file truncated at 24000 chars] ..." if len(full) >= 24000 else ""
                refine_msgs = [
                    SystemMessage(content=SYSTEM),
                    HumanMessage(content=f"Bug:\n{state.get('bug_report','')}\n\n"
                                         f"Root-cause summary:\n{text[:1500]}"),
                    HumanMessage(content=(
                        f"Full current content of `{top}` below. Output ONLY a unified diff "
                        f"that applies CLEANLY against this exact file (real context lines, "
                        f"correct path), or `N/A` if no change is warranted.\n\n"
                        f"```\n{full}{trunc}\n```")),
                ]
                patch = llm.invoke(refine_msgs)
                patch_text = patch.content if isinstance(patch.content, str) else str(patch.content)
                text += "\n\n## Patch (grounded in full file: " + top + ")\n" + patch_text
        else:
            text += "\n\n> ⚠ Patch not grounded: source folder not mounted (index-only " \
                    "mode). The draft diff above is from partial chunks — verify manually."
            needs_review = True

    # Diff grounding: check each patch hunk against the real file in the folder.
    diff_problems = []
    if r is not None and ("@@" in text or "--- " in text):
        diff_problems = validate_diffs(text, r.read_file)
        if diff_problems:
            text += "\n\n> ⚠ Diff did not validate against the source folder:\n>   - " \
                    + "\n>   - ".join(diff_problems) \
                    + "\n> Treat the patch as a described change, not an apply-ready diff."
            needs_review = True   # a non-applying diff must not be trusted

    return {
        "messages": [AIMessage(content=text)],
        "status": "completed",
        "needs_human_review": needs_review,
        "root_cause": text[:2000],
        "candidate_files": verified,
    }
