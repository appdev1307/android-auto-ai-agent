# Changes — PoC hardening pass

Fixes applied to make the agent (a) not crash mid-demo and (b) actually
demonstrate the customer/OEM-first retrieval it was built around.

## P0 — correctness / demo-blockers

1. **chunker.py `chunk_file`** — overlap was never applied
   (`start = max(end - overlap, end)` always returned `end`). Now steps back
   by `overlap` with guaranteed forward progress. `chunk_overlap` config now
   has effect. *Verified: consecutive chunks share the overlap tail.*

2. **nodes.py `agent_reason` / `finalize`** — removed the hand-sliced
   `[-12:]` / `[-16:]` message windows and the per-turn task reconstruction.
   The task is now framed **once** (main.py `_framed_task`) and the graph
   carries the conversation intact. This eliminates the orphan-tool-call 400
   (`'tool' message must follow a message with 'tool_calls'`) that triggered
   once the ReAct loop grew past ~12 messages — i.e. exactly on the complex
   bugs worth demoing.

3. **nodes.py `finalize`** — `needs_human_review` no longer depends on the
   model self-reporting an exact string. Review is **forced** whenever the
   answer touches VHAL / VSS / AIDL / SELinux / power / `hardware/interfaces`.
   Also handles markdown (`**false**`, backticks).

4. **main.py `run`** — `GraphRecursionError` is caught (was uncaught → crash).
   Added `agent.max_tool_iters` (config, default 8) so `should_continue`
   routes to `finalize` before the recursion limit is ever hit.

## P1 — ranking (the core PoC value)

5. **hybrid.py `retrieve` / `_normalize_base` / `_cross_encoder_rerank`** —
   the cross-encoder used raw logits (~[-11,+11]) blended against RRF+prior
   (~0.13), so the customer prior was ~0.5% of the final score = wiped.
   Now: RRF → min-max to [0,1] → priors added on that scale → CE squashed
   with sigmoid to [0,1] → `ce_blend` mixes two comparable [0,1] terms.
   *Verified: customer prior moves ranking (~4% of final, gap 0.22 in the
   worst-case A/B sim) instead of 0.5%.*

6. **hybrid.py** — dense similarity clamped to [0,1] (cosine distance can
   exceed 1). Unified layer tagging: removed the divergent `_layer` and use
   one canonical `guess_layer` (chunker.py) across index + all channels.

7. **hybrid.py `find_aidl` / `lookup_vss`** — now actually filter to their
   layer (`.aidl` / vss+yaml) with graceful fallback, instead of returning
   whatever `retrieve` gave.

## PoC trust / ops

8. **tools_def.py `hybrid_search`** — output now shows the ranking breakdown
   (`score`, `prior=+`, `ce=`) so you can *show* stakeholders why a `vendor/`
   file ranked first — the visible proof that customer-first is working.

9. **nodes.py** — retriever is cached per `aosp_root` (`_RETRIEVER_CACHE`)
   instead of rebuilt every graph run. No more reloading embedder +
   cross-encoder + re-fitting BM25 on each bug — keeps interactive mode warm.

10. **nodes.py `finalize`** — candidate file paths the model emits are
    verified against the tree; nonexistent ones get flagged
    (`⚠ Unverified paths`). Populates the previously-dead
    `state["candidate_files"]`.

11. **hybrid.py `read_file`** — confined to `aosp_root` (the LLM controls the
    path; `/etc/passwd` etc. is refused).

12. **requirements.txt** — dropped unused deps (typer, aiofiles, loguru);
    tree-sitter left commented as future AST-chunking (the chunker is still
    regex-based — "code-aware" is aspirational until that lands).

## Not done (deliberately out of PoC scope)

- Real tree-sitter AST chunking (option B territory).
- Structured machine-readable `patches[]` / `unit_tests[]` extraction.
- `git apply --check` patch validation harness.
- Labeled smoke/eval set — recommended next: 10–15 real bugs from your
  actual project tree, wired as a regression check before you tune ranking
  further. `data/config.yaml` `index_roots` + `customer_path_boost` still
  point at generic AOSP layout — repoint them at your real OEM tree before
  indexing.

---

# Update 2 — HIDL filter + multi-tenant knowledge (option B)

## HIDL filter in RAG (mục 4)
- `chunker.py` — added `is_hidl()` + `HIDL_PATH_MARKERS`. HIDL excluded at
  **index time** by PATH (not content/filename — those false-positive on shared
  identifiers). Extra safe signals for vendor trees: `.hal` ext + `hidl_interface`
  in `Android.bp`. Verified: catches `/vehicle/2.0/*.hal`, does NOT drop `*.aidl`.
- `guess_layer()` tags HIDL as `layer="hidl_legacy"`.
- `hybrid.py:_apply_code_priors` — `hidl_legacy` gets `prior_hidl_penalty` (0.30)
  UNLESS the query is about HIDL/migration. Kept indexable, never outranks AIDL for A14+.

## Multi-tenant knowledge store — option B, physical isolation (mục 5 + 6)
New module `retrieval/store.py` — 5 design patterns, each killing one risk:
- **Repository** `VectorStore` (Protocol) — retriever depends on interface, not chromadb.
- **Adapter** `ChromaVectorStore` — all chroma code in one class + embed-model guard.
- **Factory** `StoreProvider` — the ONE place tenant→path resolves (audit here) + path-escape guard.
- **Composite** `CompositeStore` — base ⊕ customer behind one interface; retriever is
  tenant-blind → cannot query another customer. Guard: at most ONE customer layer.
- **Facade** `KnowledgeSession` — tenant pinned at open().

Isolation is **by construction**: a session's composite only holds [base, <one customer>];
other customers aren't in the object graph. Provable at audit, not "trust the filter".

Disk layout:
    <stores_root>/_base/<ver>/{chroma, bm25_corpus.pkl, manifest.json}
    <stores_root>/<customer>/<project>/<ver>/{...}

Wiring:
- `hybrid.py:HybridRetriever` — now takes `tenant`/`store`; builds base∪customer via
  `StoreProvider` when tenant+`stores_root` set, else wraps the legacy flat index
  (`_LegacyChromaStore`) → **backward compatible**. Channels route through the store.
  Added `prior_customer_store` boost for hits from the customer store.
- `indexer.py` — tenant-aware: `--base` or `--customer/--project/--aosp-version`,
  writes `manifest.json` (embed_model + git_sha) for the guard + incremental re-index.
- `main.py` — `--customer/--project/--aosp-version` (explicit, never auto-picked).
- `state.py`/`nodes.py` — `tenant` in state; retriever cache keyed by root+tenant.
- `config.yaml` — `stores_root`, `default_tenant`, `prior_customer_store`, `prior_hidl_penalty`.

Guards verified (unit-tested): frozen Tenant, refuse 2 customer layers, refuse path escape,
embed-model mismatch raises.

## NOT applied (belongs to the OTHER repo, not this PoC)
- VSS `flatten_vss` children-unwrap fix — that bug lives in `code-codegen-aosp-llm-based`,
  not in this localization agent. See design doc mục 3.

---

# Update 3 — Aggressive index filter + incremental re-index

## Aggressive AOSP filter (chunker.py: should_index)
Full AOSP ~500GB / ~1M files but >90% is not worth vectorizing. One decisive
filter (cheapest checks first, file I/O last):
- EXCLUDE_DIR_SEGMENTS: out, prebuilts, external, test(s), cts/vts/gts, docs,
  samples, third_party, toolchain, generated, build, .git/.repo, ...
- EXCLUDE_PATH_SUBSTR: /generated/, /aidl_api/ (frozen dupes), mockito/gtest, .pb.
- EXCLUDE_NAME_SUFFIX: *test.java, *_pb2.py, .pb.h/.pb.cc, ...
- MAX_INDEX_FILE_BYTES = 400KB (skip generated tables / minified blobs)
- plus existing HIDL + CODE_EXTS gates
Effect: a ~500GB tree collapses to a few GB of real source → 1 GPU indexes in
hours, no Spark needed. Verified on synthetic tree (source kept, every noise
category dropped).

## Incremental re-index by git SHA (indexer.py)
`--incremental`: instead of rebuilding, diff the tree's current HEAD against the
SHA recorded in manifest.json and touch only what changed.
- `_git_changed(root, old, new)` → (changed_or_added, deleted) via `git diff --name-status`.
- Deletes old chunks of touched files from BOTH Chroma (delete where path=…) and
  the BM25 corpus pickle, then re-chunks + upserts changed files, appends BM25.
- Re-applies `should_index` to changed files (a file may have become excluded).
- Falls back to full rebuild if git can't answer (shallow clone missing old SHA)
  or on --reset. Verified: A(modified)+C(added) re-indexed, B(deleted) removed;
  missing-SHA → None → full rebuild.

Usage:
    # first time (full)
    python -m retrieval.indexer --aosp-root /aosp --base
    # after `repo sync` / git pull — only changed files:
    python -m retrieval.indexer --aosp-root /aosp --base --incremental

Cuts day-to-day re-index from ~1M files to the few thousand that actually changed.

---

# Update 4 — Two-tier filter + OEM-patch capture (fix: filter dropped OEM patches)

Problem: the aggressive base filter dropped OEM patches that live in test/,
external/, generated/ — and any OEM edit straight into frameworks/base can't be
caught by directory-name rules at all.

## Two-tier should_index(path, mode)
- `mode="base"` (AOSP upstream): aggressive filter, unchanged.
- `mode="customer"` (customer overlay): permissive — keeps test/external/generated,
  drops only hard junk (out/, .git, node_modules, binaries, oversized, HIDL).
- `indexer.py` picks mode automatically: `--customer` → customer, `--base` → base.
- `iter_files(..., mode=...)` threads it through.
Verified: customer files in tests/external/generated → dropped by base, KEPT by customer;
build junk + HIDL → dropped by both.

(Note: an earlier `--since-upstream` flag was also added here, then removed in
Update 5 — it blurred the tool/fetch boundary. `--customer` mode already keeps
these files. See Update 5.)

---

# Update 5 — Clarify boundary: the tool never fetches source

Removed `--since-upstream` (and its `oem_patched_files` helper). It assumed an
upstream ref / git history to diff against, which blurred the boundary: source
fetching is entirely the user's job. You clone/sync/export any tree (fresh AOSP
or a customer tree) however your workflow does it and point `--aosp-root` at it;
the indexer only reads + indexes, the agent only uses the store. No network,
no remotes.

Kept: `--base` / `--customer` tier filter and `--incremental` (a pure LOCAL
`git diff` between two SHAs already in the tree you provided — not a fetch).
README + indexer docstring updated to state the boundary explicitly.

---

# Update 6 — Prompt correctness (not complexity)

Prompt is intentionally simple — the signal lives in retrieval, not phrasing (same
ceiling the thesis hit with DSPy/MIPROv2). Fixed 4 correctness bugs, no added complexity:

- **Few-shot no longer teaches path fabrication.** `fewshot_localize.md` used `.../` in
  paths, implicitly training the model that abbreviated/invented paths are OK. Replaced
  with full real AOSP paths + an explicit "illustrative format only" disclaimer.
- **Grounding clause** in `system.md`: "You have NO prior knowledge of this tree; every
  path/symbol MUST come verbatim from a tool result; never invent/abbreviate a path."
  Directly targets the #1 localization failure (hallucinated paths).
- **Strict output contract**: ranked files as `N. <full/path> [layer]`, one per line, so
  finalize's path extraction + verification is reliable.
- **Honest-diff clause**: only emit a diff after read_source and only if it would apply
  cleanly; otherwise describe the change in words instead of fabricating a diff. Plus an
  explicit `needs_human_review: true|false` line forced true for VHAL/VSS/AIDL/power/SELinux.

Not changed: the real levers for patch quality (full-file context + `git apply --check`
validation loop + labeled eval) are architecture, not prompt.

---

# Update 7 — Diff validation against the downloaded folder (no git)

The agent only READS the downloaded source folder and never applies patches, so git is
not needed. What's needed is confirming the model's diff actually matches the real files
— to catch fabricated diffs (invented line content / wrong version).

`validate_diffs(text, read_file)` in `nodes.py` (pure Python):
- parses each unified-diff hunk, takes its "before" side (context + removed lines),
- reads the target file from `aosp_root` and checks that block actually exists there,
- flags: missing target file, or hunk context that doesn't match the real file.

`finalize` runs it whenever the output contains a diff; on any problem it appends a
warning and forces `needs_human_review = True` (a non-applying diff must not be trusted).
Verified: a real diff passes; a fabricated hunk and a missing-file diff are both flagged.

Replaces the earlier "git apply --check" idea — same goal (catch bad diffs), but works on
a plain read-only folder with no VCS dependency.

---

# Update 8 — Full-file context for patch generation (#1)

The first finalize pass drafts a diff from ~1200-char chunks, so its context lines are
usually wrong and the diff won't apply. Added a second pass that grounds the patch in the
REAL file:

- After localization, if the draft output contains a diff and the top candidate exists,
  read that file's FULL content from `aosp_root` (bounded to one file / ~24k chars so it
  fits the 16k model context) and ask the model to output a unified diff that applies
  cleanly against it. The grounded diff is appended as `## Patch (grounded in full file)`.
- `validate_diffs` (Update 7) then runs on the grounded diff.
- **Index-only mode** (source not mounted): the full-file pass is skipped and the draft is
  flagged "not grounded — verify manually", forcing human review.

Chain now: retrieve → localize → **full-file-grounded diff** → **diff validated vs folder**
→ human review. Prompt updated: first-pass diff is explicitly a "draft".
