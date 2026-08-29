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


def finalize(state: AgentState) -> Dict[str, Any]:
    """Ask model for final structured summary without new tools."""
    summary_prompt = HumanMessage(content="""Finalize now. No more tools.
Provide:
## Candidate files (ranked)
## Root cause
## Proposed patches (unified diff only) or N/A
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

    return {
        "messages": [AIMessage(content=text)],
        "status": "completed",
        "needs_human_review": needs_review,
        "root_cause": text[:2000],
        "candidate_files": verified,
    }
