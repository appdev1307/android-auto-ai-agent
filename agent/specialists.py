"""Per-layer specialist agents.

After the ReAct agent has gathered evidence, consult a specialist for each layer
the evidence touches. Each specialist is a separate LLM role with its OWN system
prompt (prompts/specialists/<layer>.md) tuned to that layer's artifacts and
failure modes. Their focused verdicts are handed to finalize for aggregation.

Bounded on purpose (top-N layers) so a bug doesn't fan out into a dozen calls.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, Any

from langchain_core.messages import SystemMessage, HumanMessage

_SPEC_DIR = Path(__file__).resolve().parents[1] / "prompts" / "specialists"

# layer tag (from guess_layer / retrieval) -> specialist prompt file
_LAYER_FILE = {
    "vhal": "vhal.md",
    "carservice": "carservice.md",
    "aidl": "aidl.md",
    "hmi": "hmi.md",
    "vss": "vss.md",
    "customer": "hmi.md",   # OEM overlay: treat with the HMI/app specialist by default
    "native": "vhal.md",    # native C/C++ outside vehicle/: VHAL specialist is closest
}

MAX_SPECIALISTS = 3   # cap LLM calls per bug


def _load(fname: str) -> str:
    p = _SPEC_DIR / fname
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_specialists_node(llm, get_retriever):
    """Factory: returns the graph node. `get_retriever()` yields the active
    HybridRetriever (already set for this run); `llm` is the base chat model."""

    def specialists(state: "AgentState") -> Dict[str, Any]:  # noqa: F821
        r = get_retriever()
        if r is None:
            return {"specialist_notes": []}
        bug = state.get("bug_report", "") or ""
        try:
            hits = r.retrieve(bug, top_k=15)
        except Exception:
            return {"specialist_notes": []}

        # group evidence by layer, preserving retrieval order
        by_layer: dict[str, list] = {}
        for h in hits:
            by_layer.setdefault(h.get("layer", "other"), []).append(h)

        # consult specialists for the layers that actually have evidence + a prompt
        notes = []
        consulted = 0
        for layer, lhits in by_layer.items():
            if layer not in _LAYER_FILE or consulted >= MAX_SPECIALISTS:
                continue
            sys_prompt = _load(_LAYER_FILE[layer])
            if not sys_prompt:
                continue
            evidence = "\n\n".join(
                f"FILE: {h.get('path')}\n{(h.get('content') or '')[:1200]}"
                for h in lhits[:4]
            )
            try:
                resp = llm.invoke([
                    SystemMessage(content=sys_prompt),
                    HumanMessage(content=f"Bug:\n{bug}\n\nEvidence for the {layer} layer:\n{evidence}"),
                ])
                txt = resp.content if isinstance(resp.content, str) else str(resp.content)
                notes.append({"layer": layer, "assessment": txt.strip()})
                consulted += 1
            except Exception:
                continue
        return {"specialist_notes": notes}

    return specialists


def format_specialist_notes(notes: list) -> str:
    if not notes:
        return ""
    out = ["\n\n# Specialist assessments (per layer)"]
    for n in notes:
        out.append(f"\n## [{n['layer']}]\n{n['assessment']}")
    return "\n".join(out)
