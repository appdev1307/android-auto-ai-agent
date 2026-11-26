"""Run folder + LangGraph checkpointer for the human gate and `--resume`.

runs/<run_id>/
  checkpoint.sqlite  LangGraph state (SqliteSaver) — what --resume continues from
  inputs.md          every user input, verbatim, appended as it arrives
  analysis.md        what the next step needs: committed diagnosis, consensus,
                     [XK-n], Source map, Next:. Rewritten at each stop.
  result.md          final output (after finalize)

Only derived results go to analysis.md; user text lives in inputs.md and is never
summarized (a resumed process sees nothing else from the earlier one).
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path

from agent.criteria import render_criteria
from agent.diagnosis import render_diagnosis


def new_run_id(bug: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (bug or "run").lower()).strip("-")[:32] or "run"
    return f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"


def run_dir(runs_root: str | Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise SystemExit(f"invalid run id: {run_id!r}")
    d = Path(runs_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_saver(d: Path):
    """SqliteSaver on runs/<id>/checkpoint.sqlite (survives the process, unlike
    MemorySaver). Package: langgraph-checkpoint-sqlite."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as e:  # pragma: no cover - dependency message
        raise SystemExit("human gate / --resume needs `pip install langgraph-checkpoint-sqlite`"
                         f" ({e})")
    conn = sqlite3.connect(str(d / "checkpoint.sqlite"), check_same_thread=False)
    return SqliteSaver(conn)


def append_input(d: Path, step: str, what: str, text: str | None) -> None:
    if not text:
        return
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(d / "inputs.md", "a", encoding="utf-8") as f:
        f.write(f"\n## {step} — {what} ({stamp})\n{text.rstrip()}\n")


def _source_map(state: dict, limit: int = 10) -> list[str]:
    """≤ `limit` lines `path (symbols) — role`: the committed file, the candidates,
    then the seed-retrieval hits. Paths only from the run (never invented)."""
    dx = state.get("diagnosis") or {}
    syms = ", ".join(dx.get("symbols") or [])
    rows, seen = [], set()

    def add(path, role):
        if path and path not in seen and len(rows) < limit:
            seen.add(path)
            rows.append(f"- `{path}` — {role}")

    if dx.get("file"):
        add(dx["file"], f"committed target ({dx.get('layer') or '?'}; symbols: {syms or '-'})")
    for c in dx.get("candidates") or []:
        add(c.get("path"), f"candidate [{c.get('layer') or '?'}] {c.get('why') or ''}".strip())
    for h in state.get("retrieved") or []:
        add(h.get("path"), f"seed retrieval [{h.get('layer') or '?'}]")
    return rows or ["- _(nothing grounded)_"]


def write_analysis(d: Path, state: dict, run_id: str, next_step: str) -> Path:
    dx = state.get("diagnosis") or {}
    cons = state.get("specialist_consensus") or {}
    rejected = bool(cons.get("reject_committed"))
    parts = [
        f"# Run {run_id}",
        f"Bug: {state.get('bug_report') or ''}",
        f"Next: {next_step}",
        "",
        render_diagnosis(dx, rejected=rejected) if dx else "_(no committed diagnosis)_",
        "",
        "## Specialist consensus",
        f"- status: {cons.get('status', '-')}; AGREE={cons.get('agree', 0)} (w{cons.get('w_agree', 0)}) "
        f"DISAGREE={cons.get('disagree', 0)} (w{cons.get('w_disagree', 0)}) "
        f"ABSTAIN={cons.get('abstain', 0)}; force review: {bool(cons.get('force_human_review'))}; "
        f"target rejected: {rejected}",
        "",
        render_criteria(state.get("criteria") or [],
                        True if state.get("user_decision") == "approve" else None),
        "",
        "## Source map",
        *_source_map(state),
    ]
    if state.get("user_notes"):
        parts += ["", "## User constraints (verbatim in inputs.md)", state["user_notes"]]
    p = d / "analysis.md"
    p.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return p
