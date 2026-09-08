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
    "native": "native.md",  # native services / non-vehicle HALs (C/C++)
    "selinux": "selinux.md",  # sepolicy: .te / *_contexts / macros
}

MAX_SPECIALISTS = 3   # cap LLM calls per bug


def _load(fname: str) -> str:
    p = _SPEC_DIR / fname
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_specialists_node(llm, get_retriever):
    """Factory: returns the graph node. Specialists now VALIDATE the committed
    diagnosis for their layer using the evidence the agent already gathered — they
    do NOT run their own retrieval, so they can't drift onto different files than
    the committed record."""

    def specialists(state: "AgentState") -> Dict[str, Any]:  # noqa: F821
        dx = state.get("diagnosis") or {}
        r = get_retriever()

        # Which layers to consult: the committed target's layer + candidate layers.
        layers: list[str] = []
        if dx.get("layer"):
            layers.append(dx["layer"])
        for c in dx.get("candidates", []) or []:
            if c.get("layer") and c["layer"] not in layers:
                layers.append(c["layer"])

        # Evidence for a layer, taken from the committed record + gathered paths —
        # read the real file content for the diagnosis target so the specialist
        # judges the same bytes the patch will be built from.
        committed_file = dx.get("file", "")
        target_content = ""
        if r is not None and committed_file:
            try:
                target_content = r.read_file(committed_file, max_chars=1600)
            except Exception:
                target_content = ""

        notes = []
        consulted = 0
        for layer in layers:
            if layer not in _LAYER_FILE or consulted >= MAX_SPECIALISTS:
                continue
            sys_prompt = _load(_LAYER_FILE[layer])
            if not sys_prompt:
                continue
            facts = (
                f"Committed diagnosis (single source of truth):\n"
                f"- file: {dx.get('file')}\n"
                f"- layer: {dx.get('layer')}\n"
                f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
                f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
                f"- root_cause: {dx.get('root_cause')}\n\n"
                f"Target file (excerpt):\n{target_content or '(not available)'}"
            )
            try:
                resp = llm.invoke([
                    SystemMessage(content=sys_prompt),
                    HumanMessage(content=(
                        f"Bug:\n{state.get('bug_report','')}\n\n{facts}\n\n"
                        f"Validate this diagnosis for the {layer} layer. State: do you AGREE "
                        f"the root cause and file are right for this layer? If not, what's "
                        f"missing? Reference only the symbols/paths above — do not invent.")),
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