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

---

# Update 9 — Labeled eval harness (#3) + README

New `eval/` module — you supply labels, it scores the agent:
- `eval/run_eval.py` — runs the agent on each labeled bug and computes recall@k, MRR,
  diff_validated_rate, and (with gold_diff_files) patch_file_hit_rate. Writes results.json.
- `eval/labels.example.jsonl` — label schema: {id, bug, logcat?, gold_files, gold_diff_files?}.
- Scoring uses suffix path-matching so short vs full paths still match; localization
  ranks parsed from the agent's `N. <path> [layer]` output.
- Labels are the user's to provide — gold_files come cheapest from real fix commits.
README gains an Evaluation section.

This is the ground truth every later improvement (prompt / model / LoRA / DSPy) is measured
against — without it, "better" is a guess.

---

# Update 10 — Diagnostic playbook + custom-hints mechanism

## Diagnostic playbook (skills/android_automotive.md)
Prompt told the model WHAT to output but not HOW to diagnose. Added a 5-step playbook:
symptom→layer map (logcat signatures), trace-the-data-path strategy, symptom→common-suspect
table, boundary-bug guidance (VSS↔VHAL/AIDL seams), and AOSP-vs-customer decision. Loaded
via SYSTEM like the other skills.

## Custom hints — no code edit (nodes.py load_hints + hints/)
User can add their own knowledge by dropping `*.md` into `hints/`:
- `load_hints()` globs `hints/*.md` (sorted) + optional `prompt.hint_files` from config,
  appends them to SYSTEM under an "Operator hints (custom)" header.
- `data/config.yaml` gains a `prompt:` block (`hints_dir`, `hint_files`).
- `hints/HOWTO.txt` (ignored — not .md) + `hints/example-hint.md.example` template.
Verified: `*.md` auto-load in filename order; `.txt`/`.example` ignored. Restart to apply.
README documents both.

---

# Update 11 — Related work section (README)

Documented surveyed papers: agentic RAG localization (BLAgent, Reformulate-Retrieve-
Localize, BugCerberus, RepairAgent, CoSIL, SWE-bench) and LLM+automotive/VSS/VHAL
(Automotive hallucination case study, Secure Multifaceted-RAG, ADAS event-chain codegen,
RAG for vehicle design). Stated the gap: agentic hybrid-RAG localization on full-stack
AAOS + OEM-first + per-customer IP isolation is underexplored. Natural baselines: BLAgent,
BugCerberus (localization); Secure Multifaceted-RAG (isolation design).

---

# Update 12 — AST-based chunking (tree-sitter)

Replaced the primary chunker for code files with tree-sitter AST chunking (the weakness
vs. BLAgent): cut at whole methods/functions instead of text boundaries, and prefix each
chunk with its structural path (`// File.java :: Class.method`) so both dense and BM25 see
where the code lives.

- `retrieval/chunker.py`: `_ast_chunks()` for Java + C/C++ (`method_declaration`,
  `constructor_declaration`, `function_definition`), with correct C++ name extraction
  (follows the declarator chain, ignores parameters). Large units split with overlap,
  header preserved on each piece.
- Everything else (`.aidl`, `.bp`, `.yaml`, `.json`, `.xml`) and any parse failure fall
  back to the existing regex chunker. Degrades silently if grammars aren't installed.
- `requirements.txt`: tree-sitter + tree-sitter-java + tree-sitter-cpp enabled.

Verified on real Java/C++: method-level chunks with `Class.method` headers; C++ names
correct (getValues/setValues), no parameter leakage; yaml falls back to regex.

Note: benefit is not yet quantified — needs the labeled eval (recall@k) to confirm the
gain over regex. That's the next dependency.

---

# Update 13 — AST coverage: + Kotlin, + VSS signal-tree chunking

Extended chunking to more of the full stack:
- **Kotlin** (HMI apps): tree-sitter-kotlin added; `.kt/.kts` chunk at
  function/class/object level with `File.kt :: Class.fun` headers.
- **VSS (yaml/json)**: `_vss_chunks()` parses the catalog and emits ONE chunk per leaf
  signal, keyed by the full dotted path (`Vehicle.Cabin.Seat.Row1.Position`), unwrapping
  the `children` wrapper so paths are clean (the labelling bug from the thesis). Non-VSS
  yaml/json falls back to regex.
- Java + C/C++ unchanged.

Coverage now: Java, Kotlin, C/C++ → AST; VSS yaml/json → signal-tree; `.aidl/.bp/.te/.xml`
and any failure → regex fallback. `requirements.txt`: + tree-sitter-kotlin.

Verified: Kotlin method-level chunks; VSS per-signal chunks with clean dotted paths (no
CHILDREN); Java/C++ intact.

---

# Update 14 — Syntax oracle: tree-sitter parse-check + generate→regenerate loop

C4-done-right: validate generated patches with a REAL parser, not regex/LLM guessing.

`retrieval/chunker.py`:
- `parse_ok(text, suffix)` — parses with tree-sitter, returns (valid, [errors]) by
  detecting ERROR / MISSING nodes. Unknown languages → (True,[]) (don't block; the real
  build catches the rest).
- `apply_unified_diff(original, diff)` — applies a diff in memory (no git/disk), validating
  both removed AND context lines; returns None on any context mismatch.

`agent/nodes.py`:
- `_grounded_patch_loop()` — generate a diff → apply it to the real full file in memory →
  parse-check the result → on a syntax error or apply-failure, feed the CONCRETE problem
  back and regenerate (up to `agent.patch_max_tries`, default 2). Returns the best patch +
  a note (forces human review) if it still fails after retries.
- Wired into finalize's full-file patch pass, replacing the single-shot generation.

Difference from thesis C4: the oracle is tree-sitter (a real parser) with concrete
line-level feedback, so the loop actually converges instead of regenerating blindly.
Verified: a broken diff (`int y = ;`) is caught by parse-check, fed back, and the retry
produces a clean, parsing diff.

Intended use: produce the syntactically cleanest diff possible before a full build; final
compile/link validation still happens on the real Android source tree (e.g. on GCP).

---

# Update 15 — Code-aware embeddings (Qwen3-Embedding-0.6B)

Swapped the embed model from all-MiniLM (NLP-only, doesn't understand code) to
`Qwen/Qwen3-Embedding-0.6B`:
- Code-aware, covers the full stack (Java/Kotlin/C++/AIDL) AND requirement text — one model
  for both, which the next-phase requirement-conformance work needs.
- Small (0.6B): fine on CPU at query time (while vLLM holds the GPU); indexing runs before
  vLLM so it can use the GPU. Configurable output dims.
- Same sentence-transformers interface → no code change beyond `embed_model`.
- `requirements.txt`: transformers>=4.51.0 (Qwen3-Embedding support).

**Re-index required**: new embeddings live in a different vector space; existing indexes are
invalid. Rebuild with `--reset`. The store manifest embed-model guard will refuse a
mismatched index, so this can't silently mis-rank.

Not benchmarked yet — candidate chosen by coverage/fit; measure with the eval harness after
phase 1 runs (per the roadmap).

---

# Update 16 — Per-layer specialists (multi-agent) via dedicated system prompts

The single generic prompt gave no per-layer / per-artifact guidance. Added a multi-agent
layer: after the ReAct agent gathers evidence, a specialist is consulted for each layer the
evidence touches, each with its OWN system prompt tuned to that layer's artifacts and
failure modes.

- `prompts/specialists/{vhal,carservice,aidl,hmi,vss}.md` — 5 specialist system prompts.
  Each: role + artifacts it owns + what to check + layer boundary (don't judge other layers)
  + output contract (is the root cause here? which file? why, grounded in a snippet).
- `agent/specialists.py` — `make_specialists_node(llm, get_retriever)`: groups retrieved
  evidence by layer, runs the matching specialist per layer (capped at MAX_SPECIALISTS=3 to
  bound cost), returns per-layer assessments. `format_specialist_notes` renders them.
- Graph: `agent →(should_continue)→ tools | specialists`, `specialists → finalize`. Finalize
  folds the specialist assessments into its context before ranking/patching.
- `tools_def.get_retriever()` added; `state.specialist_notes` added; main seeds it.

Multi-agent = a router-free fan-out: the ReAct generalist finds evidence, layer specialists
(distinct system prompts) judge their own layer, finalize aggregates. Still pure LLM + RAG.
Verified: graph wiring, 5 prompts (role/artifacts/boundary/output), cap, factory/formatter.

---

# Update 17 — HMI (Car UI libs) + VSS (COVESA .vspec) coverage

Notebook cell 3 now also clones:
- **HMI**: `packages/apps/Car/libs` (Car UI Library) — the HMI framework core, not just Settings.
- **VSS**: COVESA `vehicle_signal_specification` → dropped under `vendor/vss` so the customer
  priors + VSS signal-tree chunker pick it up (automotive scope already indexes `vendor/`).

chunker.py: COVESA files use the `.vspec` extension with a flat dotted-key format
(`Vehicle.Speed:` -> {type/datatype/unit/...}) and `#include` directives.
- `.vspec` added to CODE_EXTS (was skipped entirely) + _VSS_EXTS.
- `_vspec_flat_leaves()` emits one chunk per signal from the flat dotted-key form;
  `#include` lines are YAML comments so they're ignored. Nested-tree VSS (JSON/OEM catalog)
  still handled by the existing walker.
Verified: a COVESA .vspec is indexable and chunked per-signal (Vehicle.Speed, Seat.Row1.Pos).

---

# Update 18 — Fix: VSS chunker crashed on non-string YAML keys

`_looks_like_vss` / `_vspec_flat_leaves` did `"." in k` assuming string keys, but YAML
parses bare `true/false/on/off/yes/no`, numbers, and `null` as bool/int/None keys →
`TypeError: argument of type 'bool' is not iterable`, which killed a full index run at a
random config file. Guarded both with `isinstance(key, str)`.
Stress-tested against bool/number/None/list keys, empty dict, nested — no crash; VSS
(json tree + COVESA .vspec) still chunks per-signal.

---

# Update 19 — Operator hint for power/resume subscription drops

Added `hints/10-power-resume-subscription.md`.

This is the exact class of bug demonstrated by the sample:
"Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON".

The agent correctly ranked the VSS mapping + CarPropertyService and identified that
the mapping is present but the subscription is not re-established after the A15 power
path. The new hint makes the diagnostic order and preferred fix locations (client
re-registration on power policy / onResume) explicit so future runs are more likely
to propose a grounded patch instead of N/A, while still forcing human review on
VHAL/VSS/power paths.

---

# Update 20 — Correct hint vs skill separation

Removed `hints/10-power-resume-subscription.md`.

Reason: that content is **framework / AOSP 15 compliance knowledge**, not customer-
specific. Per project design (and operator feedback), `hints/` is reserved for:
- customer-specific requirements
- customer patches / naming
- chipset-specific quirks
- project-local known-good mappings or handlers (e.g. OemPowerPolicyHandler)

General diagnostic patterns (subscription drops after ignition/resume, diagnostic
order, preferred fix locations) were moved into `skills/android_automotive.md`
where framework knowledge belongs.

`hints/` stays clean for true OEM overlays only.

---

# Update 22 — Align finalize / few-shot / system with patch_and_ut

- agent/nodes.py finalize prompt now references skills/patch_and_ut.md and
  asks for concrete UT ideas (AAOS patterns only) + forced human-review wording.
- prompts/fewshot_localize.md: added Example 3 (localization + minimal patch
  description + named UT ideas) for the ignition/resume speed case.
- prompts/system.md and skills/AGENTS.md aligned with patch_and_ut compliance
  and customer-first fix location rules.

---

# Update 23 — Correctness / hardening pass (6 fixes)

Post-review bugfix batch. Three shipped first (patch-oracle / finalize retriever /
exact-scope), three follow (incremental scope / index-only paths / read guard).

## 1. Patch oracle no longer false-fails on large files
`read_file` gained `add_marker`; `finalize` reads the grounding file WITHOUT the
`... [truncated] ...` marker so it never reaches `apply_unified_diff` / `parse_ok`.
Files >24k chars used to guarantee a fake syntax error → 2 dead retries → forced
review. Now: file fits → oracle runs as before; file truncated → diff applied to the
visible portion, parse-check skipped, honest "verify on a real build" note.
(`hybrid.py:read_file`, `nodes.py:_grounded_patch_loop`/`finalize`)

## 2. finalize uses the ACTIVE retriever, not an arbitrary cached one
`finalize` picked `next(iter(_RETRIEVER_CACHE.values()))` — in a multi-tenant process
that could grab another tenant's retriever and read the wrong customer's tree. Now
uses `get_retriever()` (the one set in `init_retriever`), matching `specialists`.
Closes an agent-layer hole in the option-B isolation. (`nodes.py:finalize`)

## 3. Exact/ripgrep channel greps the indexed scope
`StoreManifest` now records `index_roots` + `scope` at build time; the retriever's
`_index_roots()` reads them so ripgrep greps exactly what was vectorized instead of
the legacy flat `index_roots`. Fallback: config scope preset → legacy list.
(`store.py`, `indexer.py` manifest write, `hybrid.py:_exact_search`)

## 4. Incremental index respects scope
Incremental only re-applied `should_index` (tier filter), not the scope roots, so a
change outside scope leaked into the store and full vs incremental diverged. Now
filters changed files with `_under_roots()` against the manifest's scope roots.
(`indexer.py`)

## 5. No false "hallucinated path" in index-only mode
`finalize` flagged EVERY candidate as "possible hallucination" when the source tree
wasn't mounted (`read_file` can't confirm existence). Now: when `source_present` is
False, paths aren't flagged (they came from tool results), just a light "not checked
against a tree" note. (`nodes.py:finalize`)

## 6. read_file path guard closed when aosp_root is "."
The escape guard was disabled entirely when `aosp_root` defaulted to "." (unset),
letting an LLM-supplied absolute path (e.g. /etc/passwd) be read. Now an unset root
confines reads to the working directory and refuses absolute / parent-escaping paths.
(`hybrid.py:read_file`)

Verified: all touched files compile; truncation + manifest round-trip + scope-root
resolution checked in isolation. Not run end-to-end (no GPU / AOSP tree / heavy deps
in the review env).

---

# Update 24 — Base-only loads the _base store without a flag (main.py default_tenant)

Base-only runs (`--customer` omitted) previously returned ZERO hits: `main.py` set
`tenant=None`, so `_init_store` fell back to the legacy flat `index_dir`
(`indexes/chroma_aaos`, different collection name) instead of the multi-tenant `_base`
store built by `--base`. The `default_tenant` in `config.yaml` was never read.

`agent/main.py` now resolves the tenant (`_load_cfg` + `_resolve_tenant`):
- `--customer <X>` → that tenant (unchanged; a customer overlay is still explicit).
- `--customer` omitted → fall back to config `default_tenant` (customer `base`) and load
  `<stores_root>/_base/<ver>` — but ONLY when that store exists on disk; otherwise return
  `None` and keep the legacy flat index (backward-compat, no crash for pre-multitenant setups).

Supersedes the Update 2 note "main.py — explicit, never auto-picked": a **customer** overlay is
still never auto-picked, but base-only now auto-resolves to `default_tenant` so the index you
built is actually used — `python -m agent.main --bug "..."` (no `--customer`) works out of the box.
README (§5 Run, Multi-tenant, Config) updated to match.

Verified: tenant-resolution branches (customer given / omit+_base exists / omit+no _base /
omit+no stores_root) checked in isolation; `main.py` compiles.

---

# Update 25 — No fake diffs; AAOS-native UT frameworks

- skills/patch_and_ut.md: hard anti-hallucination (§0); unified diff only if
  read_source succeeded; SELinux rule; unit tests must name Framework
  (JUnit4+Robolectric/instrumentation | GoogleTest+gmock | VTS) + TestName +
  setup/action/assert.
- prompts/system.md + skills/AGENTS.md + finalize summary_prompt: same rules.
- fewshot Example 3: UT section uses Framework / Target / setup-action-assert form.
- Model must not emit fabricated ---/+++/@@ when the file was not read.
---

# Update 26 — Stage 2 HIDL: hard drop (align with thesis)

Stage 2 at retrieval was a soft penalty (`prior_hidl_penalty`). Changed to a
**hard drop**, matching thesis `rag/aosp_retriever.py` (`_parse_results` + BM25
corpus filter).

`retrieval/hybrid.py` → `_apply_code_priors`:
- Removed soft penalty on `hidl_legacy`.
- After priors, any hit whose path matches HIDL markers / `.hal` / `layer==hidl_legacy`
  is **dropped** unless the query is explicitly about HIDL / migration.

`data/config.yaml`: removed unused `prior_hidl_penalty`.

Result: two hard gates only
1. Index time (`chunker.is_hidl` → `should_index` = False)
2. Retrieval time (hard drop in `_apply_code_priors`)

Note: Update 2 above still documents the original soft-penalty design; this update
supersedes that Stage-2 behavior.

# Update 27 — Vertical specialists + horizontal skill packs + agent contract

## Vertical specialists (graph nodes, capped at 3)
Extended set: vhal | aidl | binder | carservice | hmi | vss | startup_power |
frameworks (+ native, selinux when tagged).

New prompts: `prompts/specialists/{binder,startup_power,frameworks}.md`

## Router (`agent/specialists.py`)
`route_specialist_layers(diagnosis, bug, evidence_paths)`:
1. committed layer + candidate layers
2. path-prefix hints
3. bug keywords
→ unique list, max 3

## Horizontal skill packs (no extra LLM call)
Auto-loaded from `skills/*.md` (sorted). Injected into specialist system prompts
when layer/keywords/paths match:
- `30-aaos_app.md` — Car UI / CarPropertyManager / HMI lifecycle
- `40-sdv_vss.md` — COVESA VSS / signal mapping
- `50-native_hal.md` — native HAL / C++ services

## Contract (consistency among agents)
`skills/CONTRACT.md` — single source of truth rules: validate don't re-diagnose,
evidence grounding, layer boundaries, output discipline, safety.
Prepended to every specialist system prompt; also in global SYSTEM via auto-load.

## Wiring
- `nodes.py`: `load_skills()` auto-discovers `skills/*.md` (CONTRACT first);
  removed hardcoded per-file skill includes.
- `config.yaml`: `prompt.skills_dir`, `prompt.skill_files`; stack.layers extended.
- `chunker.guess_layer`: binder, startup_power, frameworks tags.
- `ALWAYS_REVIEW_LAYERS` / `SENSITIVE` include new layers.

Drop a new `skills/60-foo.md` to extend horizontal knowledge without code changes.

## Update 27b — MAX_SPECIALISTS=5 + consensus protocol

- Cap raised from 3 → **5** (`agent.max_specialists` in config, overridable).
- **Priority keyword pass**: ignition/resume/power → `startup_power`; binder death →
  `binder`; avc/selinux → `selinux` forced into the pool before path candidates fill slots.
- **Structured verdict**: specialists must start with `VERDICT: AGREE|DISAGREE|PARTIAL`.
- **Deterministic consensus** (`build_consensus`, no extra LLM):
  - majority AGREE → agree
  - DISAGREE on committed layer or DISAGREE ≥ AGREE → conflict → `force_human_review`
  - AGREE with minority DISAGREE → agree_with_dissent → human review
  - only PARTIAL → weak
- `specialist_consensus` stored on agent state; finalize surfaces summary and honours
  force_human_review.
- CONTRACT §7 documents consensus rules for all roles.

