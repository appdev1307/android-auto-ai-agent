"""Committed diagnosis: the single source of truth shared across agent roles.

The ReAct agent, specialists, patch generator and unit-test generator used to
each re-derive "what the bug is" from free text, so they could drift and
contradict each other. This module makes the diagnosis a STRUCTURED record that
is committed ONCE and validated by CODE against the evidence the agent actually
gathered this run — the LLM proposes it, deterministic code disposes of anything
not grounded. Every downstream role then binds to this record instead of
re-interpreting prose.

Nothing here calls an LLM; it is pure, testable state machinery.
"""

from __future__ import annotations
import re
import json
from typing import TypedDict, Any, Callable

from langchain_core.messages import ToolMessage, BaseMessage

_PATH_RE = re.compile(r"(?:^|\s)((?:[\w.\-]+/){1,}[\w.\-]+\.\w+)")
_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")


class Diagnosis(TypedDict, total=False):
    file: str                    # the single file to patch (grounded in evidence)
    layer: str                   # guess_layer(file) — code-derived, not LLM-claimed
    symbols: list[str]           # methods/callbacks central to the fix (grounded)
    property_ids: list[str]      # e.g. PERF_VEHICLE_SPEED (grounded)
    root_cause: str              # ONE committed statement
    candidates: list[dict]       # ranked [{path, layer, why}] (paths grounded)
    confidence: str              # "high" | "medium" | "low"
    grounding_problems: list[str]  # what the LLM claimed but code couldn't ground
    committed: bool              # True only if the target file is grounded


class Evidence(TypedDict):
    corpus: str            # concatenation of every tool result seen this run
    paths: list[str]       # distinct file paths that appeared in tool results


def collect_evidence(messages: list[BaseMessage]) -> Evidence:
    """Reconstruct the grounding corpus from the run's tool results. These are
    the only files/snippets the agent actually saw; anything outside them is not
    grounded for this run."""
    chunks: list[str] = []
    for m in messages or []:
        if isinstance(m, ToolMessage):
            c = m.content
            chunks.append(c if isinstance(c, str) else str(c))
    corpus = "\n".join(chunks)
    paths = list(dict.fromkeys(p for p in _PATH_RE.findall(corpus)))
    return {"corpus": corpus, "paths": paths}


def parse_diagnosis_json(raw: str) -> dict:
    """Extract the JSON object from an LLM answer (tolerates ``` fences / prose)."""
    if not raw:
        return {}
    s = raw.strip()
    s = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", s.strip())
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(s[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _snap_to_evidence(path: str, evidence_paths: list[str]) -> str | None:
    """Return the evidence path the LLM likely meant: exact, else a unique
    suffix/basename match. Returns None if it can't be grounded."""
    if not path:
        return None
    if path in evidence_paths:
        return path
    base = path.split("/")[-1]
    # strip a git a//b/ prefix the model may have left on
    stripped = re.sub(r"^[ab]/", "", path)
    if stripped in evidence_paths:
        return stripped
    matches = [e for e in evidence_paths if e == stripped or e.endswith("/" + base) or e.split("/")[-1] == base]
    return matches[0] if len(matches) == 1 else None


def commit_diagnosis_record(raw_json: str, evidence: Evidence,
                            guess_layer: Callable[[str], str]) -> Diagnosis:
    """Turn the LLM's proposed diagnosis into a COMMITTED, code-validated record.

    Rules (structure wins over the model):
      - `file` must be in the evidence paths (or snap to one); else not committed.
      - `layer` is ALWAYS recomputed from the path via guess_layer — never trusted
        from the LLM.
      - `symbols` / `property_ids` are kept only if they actually appear in the
        evidence corpus; the rest are dropped and recorded as grounding problems.
      - `candidates` keep only paths that appear in the evidence.
    """
    raw = parse_diagnosis_json(raw_json)
    corpus, epaths = evidence["corpus"], evidence["paths"]
    problems: list[str] = []

    file = _snap_to_evidence(str(raw.get("file", "")).strip(), epaths)
    committed = file is not None
    if not committed:
        claimed = str(raw.get("file", "")).strip()
        if claimed:
            problems.append(f"target file '{claimed}' is not in the evidence — not grounded")
        else:
            problems.append("no target file proposed")

    def _grounded_list(key: str, pattern: re.Pattern | None = None) -> list[str]:
        kept: list[str] = []
        for item in raw.get(key, []) or []:
            tok = str(item).strip()
            if not tok:
                continue
            if pattern and not pattern.fullmatch(tok):
                # e.g. a "property_id" that isn't CONSTANT_CASE — keep the check loose
                pass
            if tok in corpus:
                kept.append(tok)
            else:
                problems.append(f"{key[:-1]} '{tok}' not found in evidence — dropped")
        return list(dict.fromkeys(kept))

    symbols = _grounded_list("symbols")
    property_ids = _grounded_list("property_ids", _CONST_RE)

    candidates: list[dict] = []
    for c in raw.get("candidates", []) or []:
        if not isinstance(c, dict):
            continue
        p = _snap_to_evidence(str(c.get("path", "")).strip(), epaths)
        if p is None:
            continue
        candidates.append({"path": p, "layer": guess_layer(p),
                           "why": str(c.get("why", "")).strip()[:200]})
    # ensure the target file heads the candidate list
    if committed and not any(c["path"] == file for c in candidates):
        candidates.insert(0, {"path": file, "layer": guess_layer(file),
                              "why": "committed target"})

    if not committed:
        confidence = "low"
    elif symbols and not problems:
        confidence = "high"
    else:
        confidence = "medium"

    dx: Diagnosis = {
        "file": file or str(raw.get("file", "")).strip(),
        "layer": guess_layer(file) if committed else "",
        "symbols": symbols,
        "property_ids": property_ids,
        "root_cause": str(raw.get("root_cause", "")).strip(),
        "candidates": candidates,
        "confidence": confidence,
        "grounding_problems": problems,
        "committed": committed,
    }
    return dx


def render_diagnosis(dx: Diagnosis) -> str:
    """Deterministic markdown for the committed facts — so finalize RENDERS the
    diagnosis instead of letting the LLM restate (and drift from) it."""
    if not dx:
        return ""
    lines = ["## Candidate files (ranked)"]
    for i, c in enumerate(dx.get("candidates", []) or [], 1):
        why = f" — {c['why']}" if c.get("why") else ""
        lines.append(f"{i}. {c['path']} [{c.get('layer', '')}]{why}")
    if not dx.get("candidates"):
        lines.append("_(none grounded in evidence)_")
    lines.append("")
    lines.append("## Root cause")
    lines.append(dx.get("root_cause") or "_(not committed)_")
    facts = []
    if dx.get("symbols"):
        facts.append("symbols: " + ", ".join(dx["symbols"]))
    if dx.get("property_ids"):
        facts.append("property ids: " + ", ".join(dx["property_ids"]))
    if facts:
        lines.append("")
        lines.append("_Committed facts — " + "; ".join(facts) + "._")
    if dx.get("grounding_problems"):
        lines.append("")
        lines.append("> ⚠ Diagnosis grounding problems: "
                     + "; ".join(dx["grounding_problems"][:6]))
    return "\n".join(lines)