# AAOS / SDV Hybrid-RAG Agent

**Real hybrid RAG** (dense + BM25 + exact) + tool-calling ReAct agent for the
**Android 15 full stack**:

`HMI → CarService → AIDL/Binder → VHAL → VSS` (+ power / SELinux when relevant),
ranked **customer/OEM-first** (`vendor/`, `device/`), with **physical IP isolation
per customer** (multi-tenant, option B).

Not a prompt-only skeleton. Includes:
- Structural / AST-aware chunking + Chroma vector index + BM25 corpus
- Hybrid retrieve (dense + BM25 + ripgrep) → RRF → normalize → customer/OEM priors → cross-encoder rerank
- Legacy **HIDL hard-excluded** at index time and hard-dropped at retrieval (A14+ wants AIDL)
- Aggressive index filter (drops prebuilts/test/external/generated) + **incremental re-index** by per-project git SHA
- Multi-tenant stores: shared AOSP base + per-customer isolated overlay
- Tools: `hybrid_search`, `read_source`, `lookup_vss_signal`, `find_aidl_interface`, `find_symbol`
- ReAct tool loop (LangGraph) → **committed diagnosis** → **vertical specialists** (≤5) + consensus → finalize
- Patch grounded on the real file (apply + tree-sitter parse oracle) + AAOS-native UT ideas
- Forced human-review on VHAL / VSS / AIDL / binder / native / SELinux / power / frameworks layers
- OpenAI-compatible LLM backend (vLLM on server, **Ollama `qwen2.5-coder:32b` on Colab**)

---

## 1. Install

```bash
cd android-auto-ai-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system dependency: ripgrep (rg), and Google's `repo` tool for step 2
```

## 2. Get the AOSP source (fresh Android 15 from Google)

The agent does **not** fetch source — you download AOSP with Google's `repo` tool, then
point the indexer at it.

```bash
# install repo (once)
mkdir -p ~/bin && curl https://storage.googleapis.com/git-repo-downloads/repo > ~/bin/repo
chmod a+x ~/bin/repo && export PATH=~/bin:$PATH

# fresh Android 15 checkout
mkdir -p ~/aosp15 && cd ~/aosp15
repo init -u https://android.googlesource.com/platform/manifest -b android-15.0.0_r1
repo sync -c -j8           # -c: current branch only; full tree is large, takes hours
export AOSP_ROOT=~/aosp15
```

To save disk/time you can sync only the automotive-relevant projects instead of the full tree:

```bash
repo sync -c -j8 packages/services/Car hardware/interfaces frameworks/base
```

## 3. Build the RAG index

Index the tree you fetched (the aggressive filter drops prebuilts/test/external, so a full
tree is fine):

```bash
python -m retrieval.indexer --aosp-root $AOSP_ROOT --base --aosp-version aosp15
```

After a later `repo sync`, re-index only what changed:

```bash
python -m retrieval.indexer --aosp-root $AOSP_ROOT --base --aosp-version aosp15 --incremental
```

## 4. Start the LLM backend

**Server / workstation (vLLM example):**

```bash
bash scripts/start_vllm.sh        # Qwen2.5-Coder-32B-Instruct-AWQ, served as model.name, tool calling on, 32k
export OPENAI_API_BASE=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=dummy
```

`OPENAI_API_BASE` (env) overrides `model.api_base` in `data/config.yaml`, so any
OpenAI-compatible endpoint (vLLM, Ollama, Groq) can be swapped in without editing config;
`model.name` must be the model id that endpoint serves. The prompt alone is ~10k tokens and
`max_tokens` is 8192, so the backend context must be well above 8k (vLLM `MAX_LEN=32768`,
Ollama `OLLAMA_CONTEXT_LENGTH=32768` — the notebook sets it).

**Colab (dev):** use `AAOS_Agent_Colab.ipynb` — it starts **Ollama** (OpenAI-compatible on
`http://127.0.0.1:11434/v1`) to avoid vLLM / torch / flashinfer install issues on Colab
Python. Point `data/config.yaml` `model.api_base` at that endpoint (the notebook does this).

## 5. Run the agent

```bash
python -m agent.main --aosp-root $AOSP_ROOT --aosp-version aosp15 \
  --bug "Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON"
```

A single run **stops at the human gate** before the patch: it prints and writes
`runs/<id>/analysis.md` (committed diagnosis, specialist consensus, proposed acceptance
criteria `[XK-n]`, source map) and `runs/<id>/inputs.md` (your inputs, verbatim). Continue:

```bash
python -m agent.main --resume <id> --approve [--note "keep the public API unchanged"]
python -m agent.main --resume <id> --criteria-file my_xk.txt   # one per line, `unit: …`, `log: …`
python -m agent.main --resume <id> --reject "wrong layer"       # no patch, no unit test
```

State lives in `runs/<id>/checkpoint.sqlite` (LangGraph `SqliteSaver`), so `--resume` works
from a new process. `--no-gate` runs straight through (batch / CI); eval never gates.

`--logcat` drops lines starting with `#`: the `test_data/*.logcat` fixtures carry `# BUG:` /
`# LAYER:` answer headers that must not reach the agent.

Output: committed diagnosis (file / layer / symbols), specialist verdicts + consensus,
ranked evidence, a **grounded** unified diff when the source file was read (or N/A),
AAOS-native unit-test ideas, and a `needs_human_review` flag.

With no `--customer`, the agent runs **base-only**: it loads the shared `_base` store
from step 3 via the config `default_tenant` (customer `base`). If that store isn't built
yet it falls back to the legacy flat index (`rag.index_dir`) instead. Pass `--customer`
to add a customer overlay (see *Multi-tenant knowledge* below).

---

## Index scope (choose at runtime, not hard-coded)

How much of the stack to index is a **runtime choice**, via `--scope` (presets live in
`data/config.yaml` under `rag.scopes`):

| `--scope` | Covers | Notes |
|-----------|--------|-------|
| `automotive` (default) | Car + automotive HAL + car apps + vendor/device | AAOS stack; small, fast |
| `framework` | above **+ `frameworks/base`, `frameworks/av`** | fuller cross-layer coverage |
| `full` | whole tree (`.`) | real full-stack; needs a full checkout |

```bash
python -m retrieval.indexer --aosp-root $AOSP_ROOT --base --scope framework
```

Add or edit presets in config — no code change. `automotive` alone is **not** full-stack
(no `frameworks/base`); use `framework` or `full` when a bug crosses into the platform.

## Related work

The **technique** (agentic RAG for bug localization) is an active area; the **domain**
(LLM + VHAL/VSS automotive) is studied separately; their **intersection on the full AAOS
stack with OEM-first ranking + per-customer IP isolation** is where this project sits.

Agentic RAG / bug localization:
- **BLAgent: Agentic RAG for File-Level Bug Localization** (2026) — closest on technique:
  AST-based, path-augmented chunking + multi-perspective queries; argues static RAG lacks
  the reasoning to localize accurately. arXiv:2605.17965
- **Reformulate, Retrieve, Localize** (Caumartin & Melo) — a non-fine-tuned LLM reformulates
  queries over BM25, +35% first-file ranking vs. BM25 baseline. arXiv:2512.07022
- **Bridging Bug Localization and Issue Fixing (BugCerberus)** (TSE 2026) — hierarchical
  localize→fix; baselines RAG(BM25), Agentless(GPT-4o), FBL-BERT. arXiv:2502.15292
- Related agents: RepairAgent (2403.17134), CoSIL (2503.22424), SWE-bench (ICLR 2024).

LLM + automotive / VSS / VHAL:
- **Hallucination in LLM-Based Code Generation: An Automotive Case Study** (2025) — COVESA
  VSS signals as prompt input, measures hallucination; directly relevant to VSS naming +
  path-hallucination. arXiv:2508.11257
- **Secure Multifaceted-RAG for Enterprise** (2025) — closest on architecture: hybrid
  retrieval + confidentiality filtering + a local fine-tuned Qwen-2.5 for an automotive
  domain; overlaps our IP-isolation + customer-first + local model. arXiv:2504.13425
- **LLM-Empowered Event-Chain Code Generation for ADAS in SDV** (2025) — VSS comAPI-driven
  automotive code generation. arXiv:2511.21877
- **Adopting RAG for LLM-Aided Future Vehicle Design** (2024) — RAG + local LLMs, automotive
  privacy motivation. arXiv:2411.09590

**Gap addressed here**: agentic hybrid-RAG localization on the *full-stack AAOS/AOSP tree*
(HMI→CarService→AIDL→VHAL→VSS), ranked customer/OEM-first, HIDL-aware, with physical
per-customer IP isolation. The technique is not new; this specific domain application is
underexplored. BLAgent and BugCerberus are the natural localization baselines; Secure
Multifaceted-RAG is the natural comparison for the isolation design.

---

## Skills and custom hints

### Framework skills (`skills/*.md`, auto-loaded)

Global skills load in filename order (`00-`, `10-`, …). `CONTRACT.md` is applied first so
every role shares the same rules. No code change to add a pack — drop a new `*.md` and restart.

| File | Role |
|------|------|
| `CONTRACT.md` | Shared rules, mirroring code: committed diagnosis, grounding, specialist JSON verdict (AGREE/DISAGREE/ABSTAIN), consensus + review gate |
| `00-AGENTS.md` | Index: symptom → skill, vehicle data path |
| `10-android_automotive.md` | CarPropertyManager → VHAL diagnosis: property model, StatusCode, subscription, Car lifecycle, power policy |
| `20-patch_and_ut.md` | Patch loop, AOSP style, stable-AIDL rules, unit-test framework per layer |
| `30-aaos_app.md` | AAOS apps: Car connection lifecycle, subscriptions, Car UI library / RRO |
| `32-binder_ipc.md` | Binder: transaction buffer, death handling, stable AIDL versioning |
| `33-android_services.md` | CarService clients, FGS types (A14+), car watchdog |
| `34-aacp_projection.md` | **AACP = Android Auto + Apple CarPlay** (projection ≠ VHAL mapping), PROJECTION power component |
| `40-sdv_vss.md` | COVESA VSS ↔ VHAL mapping: entry types, units, type / change mode |
| `50-native_hal.md` | VHAL reference architecture, native clients (libvhalclient), dumpsys / VTS |
| `60-selinux_linux.md` | Vendor sepolicy placement, `vendor_` naming, neverallow, daemon checklist |

Every rule in a skill is tagged `[S#]` and resolved in that file's **Sources** section
(source.android.com, developer.android.com, COVESA docs, AOSP commits). Customer-specific
rules (OEM naming, MISRA/AUTOSAR when required) belong in `hints/`, not in skills.

### Operator hints (`hints/*.md`)

For **customer-specific** knowledge only (OEM naming, known handlers, chipset quirks):

```
hints/
  10-oem-power-handler.md
  20-vss-mapping.md
```

Files load in filename order. Restart to pick up changes. See `hints/HOWTO.txt`. Config:

```yaml
prompt:
  hints_dir: "hints"
  hint_files: []
  skills_dir: "skills"
  skill_files: []
```

Hints steer reasoning; they do not replace retrieval.

---

## Evaluation (labeled)

Measure the agent instead of guessing. You provide labels (bug → gold files); the harness
runs the agent and scores it. One schema is used everywhere — eval labels, `eval/mine_labels.py`
output, and `python -m agent.main --bug-file <record.json>` (e.g. `data/sample_bug.json`):

```json
{"id":"...","bug":"<symptom, no file names>","logcat_file":"test_data/<f>.logcat","gold_files":["<path under --aosp-root>"],"gold_diff_files":["<subset>"]}
```

- `eval/labels.example.jsonl` shows the schema with placeholders only; it is not a dataset.
  Real labels: `eval/mine_labels.py` (AOSP fix commits) or your own OEM fix commits.
- `logcat` (inline text) or `logcat_file` (relative to the label file, then the repo root);
  `#` lines are dropped in both.
- Gold paths are relative to the **indexed** tree. `run_eval` warns for every gold file not
  found under `--aosp-root` and reports `n_gold_missing_in_tree` — such items cannot be hit.
- `test_data/*.logcat` are synthetic demo fixtures with no gold labels; they are for
  `agent.main` demos, not for scoring.

Run:

```bash
python -m eval.run_eval --labels eval/labels.jsonl --aosp-root $AOSP_ROOT \
    --customer oem-a --project proj1 --k 5 --out eval/results.json
```

Without `--customer`, the eval resolves the tenant the same way as `agent.main`
(config `default_tenant` → the shared `_base` store).

Metrics: **recall@k** and **MRR** (localization), **diff_validated_rate** (fraction whose
grounded diff matched the real file), and — if you supply `gold_diff_files` —
**patch_file_hit_rate**. `gold_files` come cheapest from real fix commits in your tree's git
history (the files the fix touched). Start with 10–15 bugs; it's the ground truth every
later improvement (prompt, model, retrieval) is measured against.

### Mining gold labels from AOSP (`eval/mine_labels.py`)

```bash
python -m eval.mine_labels --aosp-root $AOSP_ROOT --since 2024-12-01 \
    --out eval/labels.jsonl --review-out eval/labels_review.csv --mask-symbols
python -m eval.run_eval --labels eval/labels.jsonl --aosp-root $AOSP_ROOT --k 5
```

Clones the AOSP projects (tag-less, single branch, shallow by date) from
`android.googlesource.com` — or `--source local` for the history of a `repo sync` tree —
and keeps real bug-fix commits: `Bug:` trailer, fix-like subject, ≤3 non-test code files /
≤200 lines, not reverted, deduped by Bug ID + patch-id, every gold file indexable by the
same filters the indexer uses (HIDL excluded). Design follows SWE-bench (gold = non-test
part of the fix, `test_files` kept apart, `base_commit`), Defects4J (small isolated fixes),
Kochhar et al. ASE'14 (file/symbol names masked in `bug` to avoid "already localized"
bias), HumanEval/LiveCodeBench (`--since` after the model's release; `commit_date` kept),
and SWE-bench Verified (`--min-words`, CSV sample for manual review). Each label records
`fixed_in_index` (true / false / null when the indexed checkout is shallow). AOSP issue
trackers are mostly private, so `bug` is derived from the commit message — spot-check the
review sample before reporting numbers.

---

## Run on Colab (dev phase, no server)

For development you don't need a server or the full AOSP tree. Index the automotive
**subset once → to Google Drive**, then develop against that persistent index.

Use the notebook `AAOS_Agent_Colab.ipynb`. It:
1. mounts Drive (index persists there),
2. shallow-clones automotive-relevant projects (not full AOSP) — tag-less, single branch
   (`AOSP_BRANCH`, default `main`; the store is still named `aosp15`),
3. indexes that subset to `stores_root` on Drive — reused when present, rebuilt with
   `--reset` when missing, built before Update 29 (no `git_shas`), or `SCOPE` changed,
4. starts **Ollama** with a coding model (e.g. `qwen2.5-coder:32b` or a lighter 14B) at
   `OLLAMA_CONTEXT_LENGTH=32768`, warms it up with a preflight check, and runs the agent
   (base-only, no `--customer` needed),
5. optionally mines gold labels (`eval/mine_labels.py`) and runs `eval/run_eval.py`,
   writing labels/results to Drive.

**Why Ollama on Colab:** vLLM often fails on Colab due to Python/CUDA/torch/flashinfer
wheel mismatches and long source builds. Ollama ships a prebuilt binary and still exposes
an OpenAI-compatible API the agent already uses.

**Index vs source.** The index (Chroma + BM25) is the durable asset on Drive. The source
subset is cheap and may be re-cloned per session so ripgrep + `read_source` work. Without
source, the agent still runs in **index-only mode** (dense + BM25 only; exact-search and
`read_source` degrade gracefully). Full-AOSP + `--incremental` is a production concern.

---

## Architecture

```
Bug/logcat
  → init_retriever  (base ∪ customer stores: Chroma + BM25 + rg)
  → agent (LLM + tools)  ⇄  tools (ToolNode)     # ReAct loop
  → commit_diagnosis      # single source of truth (file/layer/symbols)
  → specialists (≤ agent.max_specialists, default 5)
       + horizontal skill injection + JSON-verdict consensus (≤ agent.max_debate_rounds)
  → relocalize (once, only if owning specialists reject the target)
       → commit again if it found new candidates, else finalize
  → criteria              # 2–5 checkable [XK-n] for the committed diagnosis
  ── HUMAN GATE ──         # interrupt_before finalize; approve / replace criteria / reject
  → finalize (render diagnosis + approved [XK-n], grounded patch on the enclosing AST node,
              UT bound to the criteria, human-review)
```

Vertical specialists (examples): `vhal`, `aidl`, `binder`, `carservice`, `hmi`, `vss`,
`startup_power`, `frameworks` (+ `native` / `selinux` when tagged). Router uses committed
layer, path prefixes, and bug keywords (priority pass for power / binder / SELinux).

Four layers wired through `AgentState`: **graph** (LangGraph), **tools**
(`agent/tools_def.py`), **retrieval** (`retrieval/hybrid.py` + `store.py`), **safety**
(`finalize` + CONTRACT). See `CHANGES.md` for the full change history.

### Retrieval pipeline (`retrieval/hybrid.py`)

- **Dense**: sentence-transformers embeddings over structural/AST chunks (Chroma, cosine)
- **Sparse**: BM25 over the same chunks
- **Exact**: ripgrep over the roots that were actually indexed — recorded in the store
  manifest (`index_roots`/`scope`) so exact-search matches the vectorized scope
- **Paths**: every channel's hits are canonicalized to a path relative to the resolved
  `aosp_root`, so the same file from dense / BM25 / ripgrep fuses into one hit
- **Fuse**: file-level RRF (best rank per channel, not summed over chunks) → **min-max normalize to [0,1]** → additive customer/OEM + layer priors →
  cross-encoder rerank with **sigmoid-normalized** CE scores blended against the
  prior-boosted base — so the customer-first boost survives rerank
- **HIDL (two hard gates)**:
  1. Index time: `chunker.is_hidl` → `should_index = False` (path markers, `.hal`, `hidl_interface`)
  2. Retrieval time: hard **drop** any slipped `hidl_legacy` / HIDL path hit unless the
     query is explicitly about HIDL / migration

### Index filter & incremental (`retrieval/chunker.py`, `indexer.py`)

- `should_index(path, mode, root)` — `base` mode drops prebuilts/test/external/generated/oversized;
  `customer` mode keeps almost everything (customer patches land anywhere). Segments are
  judged **relative to the AOSP root**, so a checkout under e.g. `/mnt/external/aosp` is not
  filtered out wholesale.
- `--incremental` — a `repo sync` tree has one git repo **per project**, so the manifest
  records one SHA per project (`git_shas`, from `.repo/project.list` or a scan of the scope
  roots). Each project is `git diff`-ed against its last indexed SHA; only changed/added/
  deleted files are touched in Chroma and BM25. A project whose old SHA is missing
  (shallow clone) falls back to a full rebuild.
- A full build always starts from an empty Chroma collection (no stale chunks of
  deleted/shrunk files left next to a freshly written BM25 corpus).
- Manifest records `embed_model`, `git_sha` / `git_shas`, and `index_roots`/`scope`.

### Tools (`agent/tools_def.py`)

| Tool | Role |
|------|------|
| `hybrid_search` | dense + BM25 + exact, ranked (shows `score / prior / ce` breakdown) |
| `read_source` | file read, sandboxed to `aosp_root` |
| `lookup_vss_signal` | VSS/catalog/mapping (filtered to vss/yaml) |
| `find_aidl_interface` | AIDL (filtered to `.aidl`) |
| `find_symbol` | symbol-oriented retrieve |

---

## Multi-tenant knowledge (option B — physical IP isolation)

Each customer/OEM gets a physically separate store; base AOSP is shared read-only.

```
indexes/stores/
  _base/aosp15/            # shared AOSP, built once
  oem-a/proj1/aosp15/      # isolated customer overlay
  oem-b/proj2/aosp15/
```

Index a customer tree you fetched, then run the agent against it:

```bash
python -m retrieval.indexer --aosp-root /oem/tree --customer oem-a --project proj1
python -m agent.main --bug "..." --customer oem-a --project proj1 --aosp-version aosp15
# omit --customer => base-only: loads _base via config default_tenant
# (falls back to the legacy flat index if _base hasn't been built)
```

A session only ever loads `_base` + **one** customer store, so another customer's code is
never in the process — isolation is by construction, not "trust the filter". Built with five
patterns in `retrieval/store.py`: Repository (`VectorStore`), Adapter (`ChromaVectorStore`),
Factory (`StoreProvider` — the one place tenant→path resolves), Composite (`CompositeStore`),
Facade (`KnowledgeSession`). Guards fail closed: at most one customer layer, path sandboxed
to root, embed-model mismatch raises.

---

## Config

`data/config.yaml` — model endpoint, `stores_root`, `default_tenant` (tenant when
`--customer` is omitted; `customer: base` => base-only), index scopes, embed model, ranking
weights (`ce_blend`, `prior_customer`, `prior_customer_store`), `max_tool_iters`,
`max_specialists` (default 5), safety (`auto_apply: false`), `prompt.skills_dir` /
`prompt.hints_dir`. Changing `embed_model` invalidates existing indexes — rebuild with
`--reset`.

---

## Roadmap — next phase (requirement conformance)

Current PoC checks **syntax** (parse-check) and **semantic-framework** (AAOS layer/contract
correctness). The next phase adds **semantic-requirement**: does the code match the OEM's
own spec (VSS spec, HMI design, logic diagram)? This reuses most of what's here —
multi-tenant store (isolated OEM specs), VSS chunker, hybrid RAG, ReAct agent — following
the 2-agent `ruleMiner → codeAuditor` pattern, with a verification filter to fight LLM
over-correction (a documented failure mode for code-vs-spec judgement).

Planned order (start small, measure, then expand):

1. **Pick a machine-readable spec form** — start with VSS spec (yaml/json; easy). Defer
   HMI design / logic diagrams (usually images/PDF — the hardest blocker).
2. **Minimal ruleMiner + codeAuditor for VSS spec** — e.g. "signal X must update ≤ 100 ms /
   range / mandatory" vs the code, reusing the existing RAG + agent.
3. **Verification filter** — validate the model's verdict with tests / counterfactuals to
   cut false positives (over-correction bias).
4. **Small conformance eval** — a few labeled spec-violations; measure before expanding.
5. Only once conformance works → HMI / logic-diagram specs (hard) and full requirement
   decomposition + traceability (customer → system → SW → HLD → LLD, the V-model).

Principle: chop into per-layer PoCs, each measurable, before taking on more. Requirement
decomposition is a whole requirements-engineering system, not a single feature — don't
build the whole chain at once. See `NEXT_PHASE.md` for the fuller write-up.
