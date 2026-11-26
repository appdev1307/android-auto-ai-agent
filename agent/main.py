#!/usr/bin/env python3
"""SDV / Android 15 full-stack agent — hybrid RAG, multi-tenant knowledge (option B)."""

import os
import sys
import json
import argparse
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from langchain_core.messages import HumanMessage
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from langgraph.errors import GraphRecursionError

from agent.graph import build_graph
from agent.state import AgentState
from agent import checkpoint as ckpt
from agent.criteria import load_criteria_file, render_criteria

RUNS_ROOT = Path(__file__).resolve().parents[1] / "runs"
# init+seed (2) + ReAct ≤ 2*max_tool_iters + commit/specialists/relocalize passes
# + criteria + finalize. 25 was already reachable without the criteria node.
RECURSION_LIMIT = 40

console = Console()


def _load_cfg() -> dict:
    p = Path(__file__).resolve().parents[1] / "data" / "config.yaml"
    try:
        return yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}


def _resolve_tenant(args, cfg) -> dict | None:
    """Which knowledge store to load.

    --customer given   -> that tenant.
    --customer omitted -> fall back to config `default_tenant` (customer "base"
      by convention) so base-only actually loads the multi-tenant `_base` store
      instead of the empty legacy index_dir. Only when that `_base` store exists;
      otherwise return None and let _init_store use the legacy flat index
      (backward compat — no crash for pre-multitenant setups).
    """
    if args.customer:
        return {"customer": args.customer, "project": args.project,
                "aosp_version": args.aosp_version}
    rag = (cfg or {}).get("rag", {})
    dt = rag.get("default_tenant") or {}
    stores_root = rag.get("stores_root")
    if stores_root and dt.get("customer"):
        base_dir = Path(stores_root) / "_base" / args.aosp_version
        if base_dir.exists():
            return {"customer": dt["customer"], "project": args.project,
                    "aosp_version": args.aosp_version}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bug", type=str, help="Bug description")
    ap.add_argument("--logcat", type=str, help="Path to logcat file")
    ap.add_argument("--bug-file", type=str, default=None,
                    help="JSON (or one-line JSONL) record in the eval label schema: "
                         "bug, logcat | logcat_file; gold fields are ignored here")
    ap.add_argument("--aosp-root", type=str, default=os.environ.get("AOSP_ROOT"))
    # Tenant routing (IP isolation). Explicit --customer wins; when omitted we
    # fall back to config default_tenant so base-only loads the _base store.
    ap.add_argument("--customer", type=str, default=None,
                    help="Customer/OEM. Omit => base-only (config default_tenant).")
    ap.add_argument("--project", type=str, default="default")
    ap.add_argument("--aosp-version", type=str, default="aosp15")
    ap.add_argument("--interactive", action="store_true")
    # Human gate (default ON for a single run): stop after diagnosis + specialists +
    # proposed [XK-n], write runs/<id>/analysis.md, exit. Continue with --resume.
    ap.add_argument("--no-gate", action="store_true",
                    help="run straight through to the patch (batch / CI / eval)")
    ap.add_argument("--run-id", type=str, default=None, help="name of runs/<id>/ (default: auto)")
    ap.add_argument("--runs-dir", type=str, default=str(RUNS_ROOT))
    ap.add_argument("--resume", type=str, default=None, metavar="RUN_ID",
                    help="continue a gated run; needs one of --approve/--criteria-file/--reject")
    ap.add_argument("--approve", action="store_true", help="accept the proposed criteria")
    ap.add_argument("--criteria-file", type=str, default=None,
                    help="replace the criteria: one per line, optional `kind: ` prefix")
    ap.add_argument("--reject", type=str, default=None, metavar="REASON",
                    help="stop the run here: no patch, no unit test")
    ap.add_argument("--note", type=str, default=None,
                    help="constraint for the patch (kept verbatim, passed to patch + UT)")
    args = ap.parse_args()

    cfg = _load_cfg()
    tenant = _resolve_tenant(args, cfg)

    if args.resume:
        return resume(args)

    gate = not (args.no_gate or args.interactive)
    graph = build_graph() if not gate else None

    if args.interactive:
        who = tenant["customer"] if tenant else "base-only"
        console.print(f"[bold green]AAOS/SDV Hybrid-RAG Agent[/bold green] "
                      f"[dim]tenant={who}[/dim] (quit to exit)")
        while True:
            bug = console.input("[cyan]bug> [/cyan]")
            if bug.lower() in ("quit", "exit", "q"):
                break
            run(graph, bug, args.aosp_root, None, tenant)
    else:
        rec = load_bug_record(args.bug_file) if args.bug_file else {}
        bug = args.bug or rec.get("bug") \
            or "Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON"
        if args.logcat:
            log = _read_logcat(args.logcat)
        else:
            log = resolve_logcat(rec, Path(args.bug_file).parent if args.bug_file else None)
        if not gate:
            run(graph, bug, args.aosp_root, log, tenant)
            return
        run_id = args.run_id or ckpt.new_run_id(bug)
        d = ckpt.run_dir(args.runs_dir, run_id)
        stale = [d / n for n in ("checkpoint.sqlite", "inputs.md", "analysis.md", "result.md")
                 if (d / n).exists()]
        if stale:  # same --run-id again = a new run; the old thread would mix messages
            console.print(f"[yellow]run {run_id} exists — starting over (old files replaced)[/yellow]")
            for f in stale:
                f.unlink()
        ckpt.append_input(d, "start", "bug", bug)
        if args.bug_file:
            ckpt.append_input(d, "start", "bug file", args.bug_file)
        if args.logcat:
            ckpt.append_input(d, "start", "logcat file", args.logcat)
        ckpt.append_input(d, "start", "tenant", json.dumps(tenant) if tenant else "base-only")
        graph = build_graph(gate=True, checkpointer=ckpt.open_saver(d))
        run(graph, bug, args.aosp_root, log, tenant, run_id=run_id, run_path=d)


def _read_logcat(path: str, limit: int = 50000) -> str | None:
    """Read a logcat file, dropping `#` comment lines.

    Fixture files in test_data/ start with `# BUG:` / `# LAYER:` headers that state
    the answer; feeding them to the agent leaks the root cause into retrieval and the
    prompt. Real logcat lines never start with `#`.
    """
    p = Path(path)
    if not p.exists():
        return None
    lines = [ln for ln in p.read_text(errors="ignore").splitlines()
             if not ln.lstrip().startswith("#")]
    return "\n".join(lines)[:limit]


def resolve_logcat(rec: dict, base_dir: Path | None = None) -> str | None:
    """Logcat text for a bug / label record (same rules for agent.main and eval).

    `logcat`      : inline logcat text.
    `logcat_file` : path, relative to the record file's directory, then to the repo
                    root; read with _read_logcat (comment lines dropped).
    """
    if rec.get("logcat"):
        return "\n".join(ln for ln in str(rec["logcat"]).splitlines()
                         if not ln.lstrip().startswith("#"))
    lf = rec.get("logcat_file")
    if not lf:
        return None
    repo = Path(__file__).resolve().parents[1]
    for cand in ([base_dir / lf] if base_dir else []) + [Path(lf), repo / lf]:
        if cand.exists():
            return _read_logcat(str(cand))
    raise FileNotFoundError(f"logcat_file not found: {lf}")


def load_bug_record(path: str) -> dict:
    """One bug record from a .json object or the first line of a .jsonl file.
    Accepts the legacy key `bug_report` for `bug`."""
    text = Path(path).read_text()
    try:
        rec = json.loads(text)
    except json.JSONDecodeError:
        rec = json.loads(next(l for l in text.splitlines() if l.strip()))
    if "bug" not in rec and "bug_report" in rec:
        rec["bug"] = rec["bug_report"]
    if not rec.get("bug"):
        raise SystemExit(f"{path}: record has no 'bug'")
    return rec


def _framed_task(bug: str, log: str | None) -> str:
    return f"""Android 15 AAOS/SDV full-stack bug.

Bug report:
{bug}

Logcat (optional):
{(log or '')[:6000]}

Tasks:
1) Use tools to gather evidence (prefer customer/vendor paths, then CarService/AIDL/VHAL/VSS/HMI).
2) Rank candidate files by layer.
3) State root-cause hypothesis.
4) If confident, propose minimal unified diffs.
"""


def _config(run_id: str | None) -> dict:
    c = {"recursion_limit": RECURSION_LIMIT}
    if run_id:
        c["configurable"] = {"thread_id": run_id}
    return c


def _stop_at_gate(graph, run_id: str, d: Path) -> None:
    """Graph paused before finalize: write analysis.md and tell the user how to go on."""
    st = graph.get_state(_config(run_id)).values
    p = ckpt.write_analysis(d, st, run_id, next_step="finalize (patch + unit test) — "
                            "waiting for --approve / --criteria-file / --reject")
    console.print(Markdown(p.read_text(encoding="utf-8")))
    console.print(
        f"\n[bold yellow]Human gate[/bold yellow] — review {p}\n"
        f"  approve : python -m agent.main --resume {run_id} --approve [--note \"...\"]\n"
        f"  replace : python -m agent.main --resume {run_id} --criteria-file my_xk.txt\n"
        f"  reject  : python -m agent.main --resume {run_id} --reject \"reason\"")


def resume(args) -> None:
    d = Path(args.runs_dir) / args.resume
    if not (d / "checkpoint.sqlite").exists():
        raise SystemExit(f"no checkpoint for run {args.resume!r} in {args.runs_dir}")
    choices = [bool(args.approve), bool(args.criteria_file), args.reject is not None]
    if sum(choices) != 1:
        raise SystemExit("--resume needs exactly one of --approve / --criteria-file / --reject")
    graph = build_graph(gate=True, checkpointer=ckpt.open_saver(d))
    cfg = _config(args.resume)
    snap = graph.get_state(cfg)
    if not snap.next:
        raise SystemExit(f"run {args.resume!r} is not waiting at the gate (already finished)")
    if "finalize" not in snap.next:
        raise SystemExit(f"run {args.resume!r} stopped at {snap.next}, not at the gate")
    st = snap.values

    update: dict = {}
    if args.reject is not None:
        ckpt.append_input(d, "gate", "reject", args.reject or "(no reason)")
        update = {"user_decision": "reject", "user_notes": args.reject}
    else:
        update = {"user_decision": "approve"}
        if args.criteria_file:
            crit = load_criteria_file(args.criteria_file)
            if not crit:
                raise SystemExit(f"{args.criteria_file}: no criteria found")
            ckpt.append_input(d, "gate", "criteria file " + args.criteria_file,
                              Path(args.criteria_file).read_text(encoding="utf-8"))
            update["criteria"] = crit
        else:
            ckpt.append_input(d, "gate", "approve", "proposed criteria accepted as shown:\n"
                              + render_criteria(st.get("criteria") or [], None))
        if args.note:
            ckpt.append_input(d, "gate", "note", args.note)
            update["user_notes"] = args.note
    graph.update_state(cfg, update)

    # The retriever lives in process memory, not in the checkpoint: rebuild it from
    # the stored aosp_root / tenant before finalize (it reads the target file).
    from agent.nodes import _get_retriever
    from agent.tools_def import set_retriever
    set_retriever(_get_retriever(st.get("aosp_root") or os.environ.get("AOSP_ROOT"),
                                 st.get("tenant")))

    result = graph.invoke(None, config=cfg)
    _print_result(result)
    after = {**st, **update, **result}
    ckpt.write_analysis(d, after, args.resume, next_step="done — result.md")
    last = result["messages"][-1]
    (d / "result.md").write_text(getattr(last, "content", str(last)), encoding="utf-8")
    console.print(f"[dim]saved {d / 'result.md'}[/dim]")


def _print_result(result: dict) -> None:
    last = result["messages"][-1]
    content = getattr(last, "content", str(last))
    console.print(Markdown(content if isinstance(content, str) else str(content)))
    review = result.get("needs_human_review")
    color = "red" if review else "green"
    console.print(
        f"\n[yellow]status={result.get('status')}[/yellow] "
        f"[{color}]needs_human_review={review}[/{color}] "
        f"[dim]files={len(result.get('candidate_files') or [])} "
        f"iters={result.get('iterations')}[/dim]"
    )


def run(graph, bug: str, aosp_root: str | None, log: str | None, tenant: dict | None = None,
        run_id: str | None = None, run_path: Path | None = None):
    console.print(f"\n[bold]{bug}[/bold]\n")
    # Seed the task framing ONCE as the first human turn, then let the graph
    # carry the conversation intact (no per-turn reconstruction/slicing).
    state: AgentState = {
        "messages": [HumanMessage(content=_framed_task(bug, log))],
        "bug_report": bug,
        "logcat_snippet": log,
        "aosp_root": aosp_root,
        "tenant": tenant,
        "task_type": "localize_patch",
        "evidence": [],
        "diagnosis": {},
        "candidate_files": [],
        "root_cause": None,
        "patches": [],
        "unit_tests": [],
        "status": "start",
        # Start False: review is RAISED by real triggers (ungrounded diagnosis,
        # specialist dissent, safety-critical layer, unverified patch). Seeding
        # True made the flag always True and the metric meaningless.
        "needs_human_review": False,
        "iterations": 0,
        "relocalize_count": 0,
        "specialist_notes": [],
        "criteria": [],
        "user_decision": "",
        "user_notes": "",
    }
    try:
        result = graph.invoke(state, config=_config(run_id))
    except GraphRecursionError:
        console.print("[red]Hit recursion limit — returning partial trail.[/red]")
        return
    if run_id and graph.get_state(_config(run_id)).next:
        _stop_at_gate(graph, run_id, run_path)
        return
    _print_result(result)


if __name__ == "__main__":
    main()