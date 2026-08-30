"""Labeled eval harness for the AAOS agent.

You provide the labels (bug -> gold files), this runs the agent and scores it.
Metrics that need NO gold diff (cheap, do these first):
  - localization recall@k   : is a gold file in the agent's top-k candidates?
  - localization MRR        : 1/rank of the first gold file hit
  - path-verified rate      : fraction of candidates that exist in the tree
  - diff-applies rate       : fraction of runs whose grounded diff validated
Metric that needs a gold diff (optional):
  - patch-file hit          : does the agent's patch touch a gold_diff_file?

Label file: JSONL, one bug per line. See eval/labels.example.jsonl. Schema:
  {"id","bug","logcat"?,"gold_files":[...],"gold_diff_files":[...]?,"notes"?}

Usage:
  python -m eval.run_eval --labels eval/labels.jsonl --aosp-root $AOSP_ROOT \
      --customer oem-a --project proj1 --k 5 --out eval/results.json
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.graph import build_graph
from agent.state import AgentState
from langchain_core.messages import HumanMessage
from agent.main import _framed_task


# ── scoring helpers ──────────────────────────────────────────────
def _norm_path(p: str) -> str:
    return p.replace("\\", "/").strip().lstrip("./").lower()


def _rank_of_gold(candidates: list[str], gold: list[str]) -> int | None:
    """1-based rank of the first candidate that matches any gold file (suffix match)."""
    g = [_norm_path(x) for x in gold]
    for i, c in enumerate(candidates, 1):
        cn = _norm_path(c)
        if any(cn.endswith(x) or x.endswith(cn) for x in g):
            return i
    return None


def _extract_ranked(text: str) -> list[str]:
    """Pull ranked candidate paths from the agent's final answer, in order."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+\.\s+([^\s\[]+)", line)
        if m and "/" in m.group(1):
            out.append(m.group(1))
    if not out:  # fallback: any path-ish token, dedup preserving order
        seen = set()
        for m in re.finditer(r"((?:[\w.\-]+/){1,}[\w.\-]+\.\w+)", text):
            p = m.group(1)
            if p not in seen:
                seen.add(p); out.append(p)
    return out


def _diff_files(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"\+\+\+ [ab]/(\S+)", text)]


# ── run one bug ──────────────────────────────────────────────────
def run_one(graph, rec: dict, aosp_root, tenant, k: int) -> dict:
    state: AgentState = {
        "messages": [HumanMessage(content=_framed_task(rec["bug"], rec.get("logcat")))],
        "bug_report": rec["bug"], "logcat_snippet": rec.get("logcat"),
        "aosp_root": aosp_root, "tenant": tenant, "task_type": "localize_patch",
        "evidence": [], "candidate_files": [], "root_cause": None,
        "patches": [], "unit_tests": [], "status": "start",
        "needs_human_review": True, "iterations": 0,
    }
    try:
        result = graph.invoke(state, config={"recursion_limit": 25})
    except Exception as e:
        return {"id": rec["id"], "error": str(e)}

    final = result["messages"][-1]
    text = getattr(final, "content", "") or ""
    ranked = _extract_ranked(text)[:k]
    gold = rec.get("gold_files", [])
    rank = _rank_of_gold(ranked, gold)

    diff_ok = "@@" in text and "did not validate against the source folder" not in text
    patch_files = _diff_files(text)
    gold_diff = rec.get("gold_diff_files", [])
    patch_hit = bool(gold_diff and _rank_of_gold(patch_files, gold_diff))

    return {
        "id": rec["id"],
        "candidates": ranked,
        "gold_files": gold,
        "hit@k": rank is not None,
        "rank": rank,
        "rr": (1.0 / rank) if rank else 0.0,
        "verified_candidates": result.get("candidate_files", []),
        "produced_diff": "@@" in text,
        "diff_validated": diff_ok,
        "patch_file_hit": patch_hit if gold_diff else None,
        "needs_human_review": result.get("needs_human_review"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--aosp-root", default=None)
    ap.add_argument("--customer", default=None)
    ap.add_argument("--project", default="default")
    ap.add_argument("--aosp-version", default="aosp15")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default="eval/results.json")
    args = ap.parse_args()

    tenant = {"customer": args.customer, "project": args.project,
              "aosp_version": args.aosp_version} if args.customer else None
    recs = [json.loads(l) for l in Path(args.labels).read_text().splitlines() if l.strip()]
    graph = build_graph()

    rows = []
    for rec in recs:
        print(f"[eval] {rec['id']} …", flush=True)
        rows.append(run_one(graph, rec, args.aosp_root, tenant, args.k))

    scored = [r for r in rows if "error" not in r]
    n = len(scored) or 1
    summary = {
        "n": len(rows),
        "n_scored": len(scored),
        "errors": [r for r in rows if "error" in r],
        f"recall@{args.k}": round(sum(r["hit@k"] for r in scored) / n, 3),
        "mrr": round(sum(r["rr"] for r in scored) / n, 3),
        "produced_diff_rate": round(sum(r["produced_diff"] for r in scored) / n, 3),
        "diff_validated_rate": round(sum(r["diff_validated"] for r in scored) / n, 3),
    }
    ph = [r["patch_file_hit"] for r in scored if r["patch_file_hit"] is not None]
    if ph:
        summary["patch_file_hit_rate"] = round(sum(ph) / len(ph), 3)

    out = {"summary": summary, "rows": rows}
    Path(args.out).write_text(json.dumps(out, indent=2))
    print("\n=== SUMMARY ===")
    for key in [f"recall@{args.k}", "mrr", "produced_diff_rate", "diff_validated_rate",
                "patch_file_hit_rate"]:
        if key in summary:
            print(f"  {key:22} {summary[key]}")
    print(f"  scored {summary['n_scored']}/{summary['n']}  (errors: {len(summary['errors'])})")
    print(f"written -> {args.out}")


if __name__ == "__main__":
    main()
