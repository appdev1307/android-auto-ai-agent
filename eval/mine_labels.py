"""Mine gold labels (bug -> fixed files) from AOSP open-source fix commits.

Downloads AOSP projects from android.googlesource.com (or reuses a local
`repo sync` checkout), picks real bug-fix commits, and writes them in the
schema `eval/run_eval.py` reads:
  {"id","bug","logcat","gold_files","gold_diff_files","notes", ...extra}

Construction follows well-known benchmark papers:
  - SWE-bench (Jimenez et al., ICLR 2024): one instance = one real fix commit;
    the gold patch is the non-test part of the fix, test edits are kept apart
    (`test_files`); `base_commit` = parent of the fix.
  - Defects4J (Just et al., ISSTA 2014): keep small, isolated fixes; drop
    refactors, reverts and commits that were later reverted.
  - Kochhar et al., "Potential biases in bug localization" (ASE 2014):
    reports that already name the buggy file inflate localization scores,
    so file names (and optionally symbols) are masked in `bug`.
  - HumanEval (Chen et al., 2021) / LiveCodeBench (Jain et al., 2024):
    public code leaks into pretraining; keep only commits after the model's
    release (`--since`), and record `commit_date` for time-split reporting.
  - SWE-bench Verified (OpenAI, 2024): drop underspecified statements
    (`--min-words`) and export a sample for human review (`--review-out`).

Caveat: AOSP issue trackers are mostly private, so `bug` is derived from the
fix commit message (subject + body, trailers stripped, names masked). It is a
developer's description of the fix, not a user's symptom report. Spot-check
the review sample before reporting numbers.

Usage (remote download, recommended):
  python -m eval.mine_labels --aosp-root $AOSP_ROOT --since 2024-12-01 \
      --out eval/labels.jsonl --review-out eval/labels_review.csv

Usage (local repo-synced history only, no download):
  python -m eval.mine_labels --source local --aosp-root $AOSP_ROOT \
      --since 2023-01-01 --out eval/labels.jsonl

Remote mode is preferred: fixes merged on `main` after the indexed snapshot
(e.g. android-15.0.0_r1) are NOT in the index, so the indexed code still has
the bug (`fixed_in_index: false`), which is closest to SWE-bench's base commit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same filters the indexer uses, so every gold file is one the agent can retrieve.
from retrieval.chunker import (  # noqa: E402
    CODE_EXTS,
    EXCLUDE_DIR_SEGMENTS,
    EXCLUDE_NAME_SUFFIX,
    EXCLUDE_PATH_SUBSTR,
    is_hidl,
)

# ── defaults ─────────────────────────────────────────────────────
# repo name on googlesource : path in the AOSP tree : pathspec inside the repo
DEFAULT_PROJECTS = [
    "platform/packages/services/Car:packages/services/Car:",
    "platform/hardware/interfaces:hardware/interfaces:automotive",
]
REMOTE_BASE = "https://android.googlesource.com"
SHALLOW_MARGIN_DAYS = 60

# Files a patch is scored on (gold_diff_files).
PATCH_EXTS = {".java", ".kt", ".kts", ".cpp", ".cc", ".c", ".h", ".hpp", ".aidl"}
# Build plumbing: never a localization target.
BUILD_EXTS = {".bp", ".mk"}

TEST_SEGMENTS = {"test", "tests", "testing", "androidtest", "unittest", "unittests",
                 "cts", "vts", "gts", "robotests", "multivalenttests"}
TEST_NAME_RE = re.compile(r"(tests?\.(java|kt|kts)|tests?\.(cpp|cc)|_test\.(cpp|cc|c))$", re.I)

FIX_RE = re.compile(
    r"\b(fix(es|ed)?|bug|crash(es|ed)?|npe|null(pointer)?|leak(s|ed)?|race|deadlock|"
    r"anr|hang(s)?|regression|incorrect(ly)?|wrong|broken|fail(s|ed|ure)?|"
    r"not (working|updat\w*|shown|called|sent|reported)|does(n't| not)|"
    r"prevent|missing|handle|exception|invalid|stuck|timeout)\b", re.I)
SKIP_SUBJECT_RE = re.compile(
    r"^(revert|reland|merge|\[automerger\]|automerger|import translations)|"
    r"\b(refactor\w*|clean ?up|rename[ds]?|bump|lint|typo|javadoc|comment[s]?|"
    r"format(ting)?|fix(ed|es)? (the )?build|build (fix|break)|flaky|"
    r"add(ed)? (unit )?tests?|test[s]? only|update (owners|readme|test))\b", re.I)
REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
BUG_TRAILER_RE = re.compile(r"^\s*Bug\s*:\s*(.+)$", re.M | re.I)

TRAILER_KEYS = {
    "bug", "test", "flag", "change-id", "signed-off-by", "merged-in", "reviewed-on",
    "reviewed-by", "tested-by", "commit-queue", "auto-submit", "presubmit-ready",
    "ignore-aosp-first", "exempt-from-owner-approval", "relnote", "fixes",
    "cherry-picked-from", "co-authored-by", "acked-by", "cc", "api-coverage-bug",
    "lowcoverage-reason", "no-typo-check", "bypass-cheops-checks",
}
TRAILER_LINE_RE = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:\s")


# ── git helpers ──────────────────────────────────────────────────
def git(cwd: Path, *args: str, check: bool = True, stdin: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True,
                       text=True, errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{r.stderr.strip()}")
    return r.stdout


def ensure_clone(remote_base: str, repo: str, branch: str, since: str,
                 cache: Path) -> tuple[Path, str]:
    """Shallow, tag-less, single-branch clone limited to history after `since`.

    The shallow cut is placed SHALLOW_MARGIN_DAYS before `since`, so the first
    fix inside the window still has its parent (needed for base_commit/diff).
    """
    since = (date.fromisoformat(since[:10]) - timedelta(days=SHALLOW_MARGIN_DAYS)).isoformat()
    dest = cache / repo.replace("/", "__")
    url = f"{remote_base.rstrip('/')}/{repo}"
    if not (dest / ".git").exists():
        cache.mkdir(parents=True, exist_ok=True)
        print(f"[mine] clone {url} ({branch}, since {since}) -> {dest}", flush=True)
        git(cache, "clone", "--no-tags", "--single-branch", "--branch", branch,
            f"--shallow-since={since}", url, str(dest))
    else:
        print(f"[mine] fetch {url} ({branch})", flush=True)
        git(dest, "fetch", "--no-tags", f"--shallow-since={since}", "origin", branch)
    # one more level so commits at the shallow cut still get their parent
    git(dest, "fetch", "--no-tags", "--deepen=1", "origin", branch, check=False)
    return dest, f"origin/{branch}"


def iter_commits(gdir: Path, ref: str, since: str, until: str | None,
                 pathspec: str) -> list[dict]:
    args = ["log", ref, "--no-merges", f"--since={since}",
            "--format=%H%x1f%P%x1f%cI%x1f%B%x1e"]
    if until:
        args.append(f"--until={until}")
    args += ["--", pathspec or "."]
    out = []
    for rec in git(gdir, *args).split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, date, body = rec.split("\x1f", 3)
        out.append({"sha": sha, "parents": parents.split(), "date": date,
                    "msg": body.strip()})
    out.reverse()  # oldest first: dedup keeps the original fix, not cherry-picks
    return out


def commit_numstat(gdir: Path, sha: str) -> list[tuple[int | None, int | None, str]]:
    rows = []
    for line in git(gdir, "show", "--numstat", "--no-renames", "--format=", sha).splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, p = parts
        rows.append((None if a == "-" else int(a), None if d == "-" else int(d), p))
    return rows


def patch_id(gdir: Path, sha: str) -> str | None:
    diff = git(gdir, "show", sha, check=False)
    out = git(gdir, "patch-id", "--stable", stdin=diff, check=False).split()
    return out[0] if out else None


_ATTRS_FILE: Path | None = None


def _funcname_attrs() -> Path:
    """gitattributes enabling git's built-in java/kotlin/cpp hunk-header drivers."""
    global _ATTRS_FILE
    if _ATTRS_FILE is None:
        fd, name = tempfile.mkstemp(prefix="mine_labels_", suffix=".gitattributes")
        with os.fdopen(fd, "w") as f:
            f.write("*.java diff=java\n*.kt diff=kotlin\n*.kts diff=kotlin\n"
                    "*.c diff=cpp\n*.cc diff=cpp\n*.cpp diff=cpp\n*.h diff=cpp\n"
                    "*.hpp diff=cpp\n")
        _ATTRS_FILE = Path(name)
    return _ATTRS_FILE


def hunk_symbols(gdir: Path, sha: str, files: list[str]) -> set[str]:
    """Function names from hunk headers of the code files (for --mask-symbols)."""
    syms: set[str] = set()
    if not files:
        return syms
    diff = git(gdir, "-c", f"core.attributesFile={_funcname_attrs()}", "show", "-U0",
               "--format=", sha, "--", *files, check=False)
    for line in diff.splitlines():
        if line.startswith("@@"):
            ctx = line.split("@@", 2)[-1]
            names = re.findall(r"([A-Za-z_]\w{3,})\s*\(", ctx)
            if names:
                syms.add(names[-1])
    return syms


def is_ancestor(proj_dir: Path, sha: str) -> bool | None:
    """Is the fix already in the indexed checkout? None if not a git checkout."""
    if not (proj_dir / ".git").exists():
        return None
    r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                       cwd=proj_dir, capture_output=True, text=True)
    return r.returncode == 0


# ── file classification (mirrors retrieval.chunker.should_index, base mode) ──
def is_test(aosp_path: str) -> bool:
    pp = PurePosixPath(aosp_path)
    if {s.lower() for s in pp.parts} & TEST_SEGMENTS:
        return True
    return bool(TEST_NAME_RE.search(pp.name))


def indexable(aosp_path: str) -> bool:
    """Path-only version of chunker.should_index(mode='base'); size cap not checked."""
    pp = PurePosixPath(aosp_path)
    p = "/" + aosp_path.lower()
    if pp.suffix.lower() not in CODE_EXTS:
        return False
    if is_hidl(p):
        return False
    if {s.lower() for s in pp.parts} & EXCLUDE_DIR_SEGMENTS:
        return False
    if any(s in p for s in EXCLUDE_PATH_SUBSTR):
        return False
    if any(p.endswith(s) for s in EXCLUDE_NAME_SUFFIX):
        return False
    return True


# ── statement cleaning / leak masking ────────────────────────────
def clean_message(msg: str, subject_only: bool) -> str:
    lines = msg.splitlines()
    if subject_only:
        lines = lines[:1]
    keep = []
    for line in lines:
        m = TRAILER_LINE_RE.match(line)
        if m and m.group(1).lower() in TRAILER_KEYS:
            continue
        if line.strip().lower().startswith("(cherry picked from commit"):
            continue
        keep.append(line.rstrip())
    text = "\n".join(keep)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def mask(text: str, tokens: set[str], placeholder: str) -> tuple[str, int]:
    n = 0
    for tok in sorted(tokens, key=len, reverse=True):
        pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", re.I)
        text, k = pat.subn(placeholder, text)
        n += k
    return text, n


def file_tokens(paths: list[str]) -> set[str]:
    toks: set[str] = set()
    for p in paths:
        pp = PurePosixPath(p)
        toks.add(p)
        toks.add(pp.name)
        if len(pp.stem) >= 4:
            toks.add(pp.stem)
    return toks


# ── main mining loop ─────────────────────────────────────────────
def mine_project(spec: str, args, seen_bugs: set[str], seen_pids: set[str],
                 funnel: Counter) -> list[dict]:
    parts = spec.split(":")
    if len(parts) < 2 or not parts[0]:
        raise SystemExit(f"bad --project spec: {spec!r} (want repo:aosp_path[:pathspec])")
    repo, proj_path, pathspec = (parts + [""])[:3]
    proj_path = proj_path.strip("/")
    aosp_root = Path(args.aosp_root).expanduser() if args.aosp_root else None

    if args.source == "remote":
        gdir, ref = ensure_clone(args.remote_base, repo, args.branch, args.since,
                                 Path(args.cache_dir).expanduser())
    else:
        if not aosp_root:
            raise SystemExit("--source local needs --aosp-root")
        gdir, ref = aosp_root / proj_path, "HEAD"
        if not (gdir / ".git").exists():
            print(f"[mine] skip {proj_path}: not a git checkout under {aosp_root}")
            return []

    commits = iter_commits(gdir, ref, args.since, args.until, pathspec)
    reverted = {m.lower() for c in commits for m in REVERTS_RE.findall(c["msg"])}
    print(f"[mine] {repo}: {len(commits)} commits touching '{pathspec or '.'}'", flush=True)

    out = []
    for c in commits:
        funnel["commits_seen"] += 1
        sha, msg = c["sha"], c["msg"]
        subject = msg.splitlines()[0] if msg else ""

        if not c["parents"]:
            funnel["drop_shallow_boundary"] += 1; continue
        bug_ids = sorted({b for line in BUG_TRAILER_RE.findall(msg)
                          for b in re.findall(r"\d{5,}", line)})
        if not bug_ids:
            funnel["drop_no_bug_trailer"] += 1; continue
        if SKIP_SUBJECT_RE.search(subject):
            funnel["drop_non_fix_subject"] += 1; continue
        if not FIX_RE.search(msg if not args.subject_only else subject):
            funnel["drop_no_fix_keyword"] += 1; continue
        if any(sha.startswith(r) for r in reverted):
            funnel["drop_reverted_later"] += 1; continue

        rows = commit_numstat(gdir, sha)
        code, tests, other, lines = [], [], [], 0
        for a, d, p in rows:
            ap = f"{proj_path}/{p}" if proj_path else p
            ext = PurePosixPath(p).suffix.lower()
            if is_test(ap):
                tests.append(ap)
            elif ext in PATCH_EXTS:
                code.append(ap)
                lines += (a or 0) + (d or 0)
            elif ext not in BUILD_EXTS:
                other.append(ap)

        if not code:
            funnel["drop_no_code_change"] += 1; continue
        if len(code) > args.max_files:
            funnel["drop_too_many_files"] += 1; continue
        if lines > args.max_lines:
            funnel["drop_patch_too_large"] += 1; continue
        if not all(indexable(f) for f in code):
            funnel["drop_gold_not_indexable"] += 1; continue
        if args.require_test_change and not tests:
            funnel["drop_no_test_change"] += 1; continue

        if any(b in seen_bugs for b in bug_ids):
            funnel["drop_dup_bug_id"] += 1; continue
        pid = patch_id(gdir, sha)
        if pid and pid in seen_pids:
            funnel["drop_dup_patch"] += 1; continue

        in_tree = fixed = None
        if aosp_root:
            in_tree = all((aosp_root / f).is_file() for f in code)
            if args.require_in_tree and not in_tree:
                funnel["drop_gold_missing_in_tree"] += 1; continue
            fixed = is_ancestor(aosp_root / proj_path, sha)

        statement = clean_message(msg, args.subject_only)
        all_paths = [p for _, _, p in rows] + code + tests + other
        statement, n_file = mask(statement, file_tokens(all_paths), "<FILE>")
        n_sym = 0
        if args.mask_symbols:
            rel = [p for _, _, p in rows if f"{proj_path}/{p}" in code or p in code]
            statement, n_sym = mask(statement, hunk_symbols(gdir, sha, rel), "<SYMBOL>")
        if len(re.findall(r"[A-Za-z]{2,}", statement)) < args.min_words:
            funnel["drop_underspecified"] += 1; continue

        seen_bugs.update(bug_ids)
        if pid:
            seen_pids.add(pid)
        funnel["kept"] += 1
        gold_files = code + [f for f in other if indexable(f)]
        out.append({
            "id": f"{proj_path.replace('/', '-').lower() or 'aosp'}-{sha[:10]}",
            "bug": statement,
            "logcat": None,
            "gold_files": gold_files,
            "gold_diff_files": code,
            "notes": f"mined from {repo}@{sha[:12]} (commit message, names masked)",
            # extra provenance (ignored by run_eval)
            "repo": repo,
            "project_path": proj_path,
            "fix_commit": sha,
            "base_commit": c["parents"][0],
            "commit_date": c["date"],
            "bug_ids": bug_ids,
            "subject": subject,
            "test_files": tests,
            "patch_lines": lines,
            "masked": {"files": n_file, "symbols": n_sym},
            "gold_in_tree": in_tree,
            "fixed_in_index": fixed,
            "url": f"{args.remote_base.rstrip('/')}/{repo}/+/{sha}",
        })
    return out


def write_review(path: Path, recs: list[dict], n: int, seed: int) -> None:
    sample = random.Random(seed).sample(recs, min(n, len(recs)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "url", "commit_date", "subject", "bug", "gold_diff_files",
                    "verdict(keep/drop)", "reason"])
        for r in sample:
            w.writerow([r["id"], r["url"], r["commit_date"], r["subject"], r["bug"],
                        ";".join(r["gold_diff_files"]), "", ""])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=["remote", "local"], default="remote",
                    help="remote: clone from googlesource; local: use repo-synced history")
    ap.add_argument("--project", action="append", default=None,
                    help="repo:aosp_path[:pathspec], repeatable (default: Car + automotive HAL)")
    ap.add_argument("--aosp-root", default=None,
                    help="indexed AOSP tree; used to check gold files exist and fixed_in_index")
    ap.add_argument("--remote-base", default=REMOTE_BASE)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--cache-dir", default="~/.cache/aaos_label_mining")
    ap.add_argument("--since", default="2024-12-01",
                    help="only commits after this date (set after the LLM's release)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--max-files", type=int, default=3, help="max changed non-test code files")
    ap.add_argument("--max-lines", type=int, default=200, help="max +/- lines in code files")
    ap.add_argument("--min-words", type=int, default=6, help="min words in cleaned statement")
    ap.add_argument("--subject-only", action="store_true",
                    help="statement = subject line only (less leakage, less detail)")
    ap.add_argument("--mask-symbols", action="store_true",
                    help="also mask function names from the fix's hunk headers")
    ap.add_argument("--require-test-change", action="store_true",
                    help="keep only fixes that also touched tests (SWE-bench style)")
    ap.add_argument("--no-require-in-tree", dest="require_in_tree", action="store_false",
                    help="keep instances whose gold files are missing in --aosp-root")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of labels")
    ap.add_argument("--out", default="eval/labels.jsonl")
    ap.add_argument("--stats-out", default=None, help="default: <out>.stats.json")
    ap.add_argument("--review-out", default=None, help="CSV sample for manual review")
    ap.add_argument("--review-n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    projects = args.project or DEFAULT_PROJECTS
    seen_bugs: set[str] = set()
    seen_pids: set[str] = set()
    funnels: dict[str, Counter] = {}
    recs: list[dict] = []
    for spec in projects:
        f = Counter()
        recs += mine_project(spec, args, seen_bugs, seen_pids, f)
        funnels[spec] = f

    recs.sort(key=lambda r: r["commit_date"])
    if args.limit:
        recs = recs[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = sum(funnels.values(), Counter())
    stats = {
        "params": {k: v for k, v in vars(args).items()},
        "projects": projects,
        "n_labels": len(recs),
        "funnel_total": dict(total),
        "funnel_by_project": {k: dict(v) for k, v in funnels.items()},
        "fixed_in_index": dict(Counter(str(r["fixed_in_index"]) for r in recs)),
        "with_test_change": sum(bool(r["test_files"]) for r in recs),
        "date_range": [recs[0]["commit_date"], recs[-1]["commit_date"]] if recs else None,
    }
    stats_path = Path(args.stats_out or f"{out}.stats.json")
    stats_path.write_text(json.dumps(stats, indent=2))

    if args.review_out:
        write_review(Path(args.review_out), recs, args.review_n, args.seed)

    print("\n=== FUNNEL ===")
    for k, v in sorted(total.items(), key=lambda kv: (kv[0] != "commits_seen", kv[0])):
        print(f"  {k:28} {v}")
    print(f"labels   -> {out} ({len(recs)})")
    print(f"stats    -> {stats_path}")
    if args.review_out:
        print(f"review   -> {args.review_out}")


if __name__ == "__main__":
    main()
