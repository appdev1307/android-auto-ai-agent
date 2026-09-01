#!/usr/bin/env python3
"""SDV / Android 15 full-stack agent — hybrid RAG, multi-tenant knowledge (option B)."""

import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from langgraph.errors import GraphRecursionError

from agent.graph import build_graph
from agent.state import AgentState

console = Console()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bug", type=str, help="Bug description")
    ap.add_argument("--logcat", type=str, help="Path to logcat file")
    ap.add_argument("--aosp-root", type=str, default=os.environ.get("AOSP_ROOT"))
    # Tenant routing (IP isolation). MUST be explicit — never auto-picked.
    ap.add_argument("--customer", type=str, default=None,
                    help="Customer/OEM. Omit => base-only knowledge.")
    ap.add_argument("--project", type=str, default="default")
    ap.add_argument("--aosp-version", type=str, default="aosp15")
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()

    tenant = {"customer": args.customer, "project": args.project,
              "aosp_version": args.aosp_version} if args.customer else None

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
        bug = args.bug or "Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON"
        log = None
        if args.logcat and Path(args.logcat).exists():
            log = Path(args.logcat).read_text(errors="ignore")[:50000]
        run(graph, bug, args.aosp_root, log, tenant)


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
        "candidate_files": [],
        "root_cause": None,
        "patches": [],
        "unit_tests": [],
        "status": "start",
        "needs_human_review": True,
        "iterations": 0,
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
