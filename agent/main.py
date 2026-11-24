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
    args = ap.parse_args()

    cfg = _load_cfg()
    tenant = _resolve_tenant(args, cfg)

    graph = build_graph()

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
        run(graph, bug, args.aosp_root, log, tenant)


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


def run(graph, bug: str, aosp_root: str | None, log: str | None, tenant: dict | None = None):
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
    }
    try:
        result = graph.invoke(state, config={"recursion_limit": 25})
    except GraphRecursionError:
        console.print("[red]Hit recursion limit — returning partial trail.[/red]")
        return
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


if __name__ == "__main__":
    main()