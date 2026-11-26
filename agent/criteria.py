"""Acceptance criteria `[XK-n]` — proposed after the specialists, fixed at the gate.

A criterion is something a person who was not in the run can check: a log line that
must (or must no longer) appear after a given step, a property value, a screen state,
or a unit test result. "Works", "fixed", "stable" are not criteria.

Lifecycle (mirrors the human gate in agent/graph.py):
  1. `propose_criteria` (graph node) asks the LLM for 2–5 criteria bound to the
     committed diagnosis, then validates them deterministically here.
  2. The gate (interrupt before `finalize`) shows them; the user approves, replaces
     them (`--criteria-file`) or rejects the run.
  3. `finalize` renders the approved set and binds the unit test to it. Nothing after
     the gate may add, drop or reword a criterion.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = {"log", "property", "screen", "unit", "manual"}
# kinds that can be checked without a bench / vehicle; the rest needs QC on a device / bench
OFFLINE_KINDS = {"unit"}

_VAGUE = re.compile(r"^\W*(it\s+)?(works?|is\s+fixed|fixed|stable|no\s+bug|bug\s+(is\s+)?gone)\W*$", re.I)
_IDENT = re.compile(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]+)\b")

PROMPT = """Propose acceptance criteria for the COMMITTED diagnosis below. No tools.

Each criterion must be checkable by someone who was not in this run:
  - log      : a log line (tag + message text) that must / must no longer appear after a step
  - property : a VHAL / Car property value (name, area, value) after a step
  - screen   : a visible state of a named screen / widget after a step
  - unit     : a unit test assertion on a named method / symbol
  - manual   : needs a bench / vehicle / QC step you cannot express otherwise
"Works", "fixed", "stable" are NOT criteria. Use only names (tags, symbols, property ids,
files) that appear in the committed record or the evidence excerpt below; never invent.
Give 2 to 5 criteria. Include at least one that FAILS on the current code (proves the bug)
and one that guards against a regression of the normal path.

Return ONLY a JSON array, no prose:
[{"check": "<one sentence>", "kind": "log|property|screen|unit|manual",
  "how": "<the step / command / test that produces the evidence>"}]
"""


def parse_criteria(raw: str) -> list[dict]:
    """JSON array from an LLM answer (tolerates fences / prose around it)."""
    if not raw:
        return []
    s = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw.strip())
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b <= a:
        return []
    try:
        arr = json.loads(s[a:b + 1])
    except Exception:
        return []
    return [x for x in arr if isinstance(x, dict)] if isinstance(arr, list) else []


def validate_criteria(items: list[dict], grounding_text: str, limit: int = 5) -> list[dict]:
    """Deterministic cleanup: drop vague / empty items, normalize kind, renumber
    `XK-1..n`, and flag identifiers that appear nowhere in the run's evidence."""
    out: list[dict] = []
    for it in items:
        check = str(it.get("check") or "").strip()
        if len(check) < 15 or _VAGUE.match(check):
            continue
        kind = str(it.get("kind") or "manual").strip().lower()
        kind = kind if kind in KINDS else "manual"
        idents = sorted(set(_IDENT.findall(check + " " + str(it.get("how") or ""))))
        ungrounded = [t for t in idents if t not in grounding_text]
        out.append({"check": check, "kind": kind,
                    "how": str(it.get("how") or "").strip(),
                    "ungrounded": ungrounded,
                    "offline": kind in OFFLINE_KINDS})
        if len(out) >= limit:
            break
    for i, it in enumerate(out, 1):
        it["id"] = f"XK-{i}"
    return out


def render_criteria(items: list[dict], approved: bool | None) -> str:
    if approved is True:
        head = "## Acceptance criteria (approved at the gate)"
    elif approved is False:
        head = "## Acceptance criteria (proposed — NOT reviewed: gate off)"
    else:
        head = "## Acceptance criteria (proposed — waiting for approval)"
    if not items:
        return head + "\n_(none proposed — give them with `--criteria-file` at the gate)_"
    lines = [head]
    for it in items:
        tag = "" if it.get("offline") else " · QC: needs device / bench"
        lines.append(f"- [{it['id']}] ({it['kind']}{tag}) {it['check']}")
        if it.get("how"):
            lines.append(f"  - how: {it['how']}")
        if it.get("ungrounded"):
            lines.append(f"  - ⚠ not seen in this run's evidence: {', '.join(it['ungrounded'])}")
    return "\n".join(lines)


def load_criteria_file(path: str) -> list[dict]:
    """User-supplied criteria at the gate: one per line, `kind: check` or plain text
    (`#` lines ignored). Ids are assigned in order; the user's wording is kept."""
    items = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        ln = re.sub(r"^\[?XK-\d+\]?\s*[:.-]?\s*", "", ln)
        kind, check = "manual", ln
        m = re.match(r"^(log|property|screen|unit|manual)\s*:\s*(.+)$", ln, re.I)
        if m:
            kind, check = m.group(1).lower(), m.group(2).strip()
        items.append({"check": check, "kind": kind, "how": "", "ungrounded": [],
                      "offline": kind in OFFLINE_KINDS})
    for i, it in enumerate(items, 1):
        it["id"] = f"XK-{i}"
    return items
