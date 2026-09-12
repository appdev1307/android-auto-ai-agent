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
- Aggressive index filter (drops prebuilts/test/external/generated) + **incremental re-index** by git SHA
- Multi-tenant stores: shared AOSP base + per-customer isolated overlay
- Tools: `hybrid_search`, `read_source`, `lookup_vss_signal`, `find_aidl_interface`, `find_symbol`
- ReAct tool loop (LangGraph) → **committed diagnosis** → **vertical specialists** (≤5) + consensus → finalize
- Patch grounded on the real file (apply + tree-sitter parse oracle) + AAOS-native UT ideas
- Forced human-review on VHAL / VSS / AIDL / SELinux / power / binder / projection seams
- OpenAI-compatible LLM backend (vLLM on server, **Ollama on Colab**)

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
bash scripts/start_vllm.sh
export OPENAI_API_BASE=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=dummy
```

**Colab (dev):** use `AAOS_Agent_Colab.ipynb` — it starts **Ollama** (OpenAI-compatible on
`http://127.0.0.1:11434/v1`) to avoid vLLM / torch / flashinfer install issues on Colab
Python. Point `data/config.yaml` `model.api_base` at that endpoint (the notebook does this).

## 5. Run the agent

```bash
python -m agent.main --aosp-root $AOSP_ROOT --aosp-version aosp15 \
  --bug "Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON"
```

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
| `CONTRACT.md` | Shared agent rules (validate committed diagnosis; no invented paths) |
| `00-AGENTS.md` | Operating rules / customer-first |
| `10-android_automotive.md` | AAOS diagnostic playbook (logcat→layer, data path, power, SELinux) |
| `20-patch_and_ut.md` | Patch + UT rules (AOSP style; no fake diffs) |
| `30-aaos_app.md` | Native AAOS / Car UI apps |
| `32-binder_ipc.md` | Binder / AIDL IPC |
| `33-android_services.md` | CarService and Android service lifecycle |
| `34-aacp_projection.md` | **AACP = Android Auto + Apple CarPlay** (projection ≠ VHAL mapping) |
| `40-sdv_vss.md` | VSS / COVESA mapping |
| `50-native_hal.md` | Native HAL / C++ |
| `60-selinux_linux.md` | SELinux + Linux service basics |

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
runs the agent and scores it. Label file is JSONL — see `eval/labels.example.jsonl`:

```json
{"id":"bug-001","bug":"...","gold_files":["path/a.java","path/b.yaml"],"gold_diff_files":["path/a.java"]}
```

Run:

```bash
python -m eval.run_eval --labels eval/labels.jsonl --aosp-root $AOSP_ROOT \
    --customer oem-a --project proj1 --k 5 --out eval/results.json
```

Metrics: **recall@k** and **MRR** (localization), **diff_validated_rate** (fraction whose
grounded diff matched the real file), and — if you supply `gold_diff_files` —
**patch_file_hit_rate**. `gold_files` come cheapest from real fix commits in your tree's git
history (the files the fix touched). Start with 10–15 bugs; it's the ground truth every
later improvement (prompt, model, LoRA) is measured against.

---

## Run on Colab (dev phase, no server)

For development you don't need a server or the full AOSP tree. Index the automotive
**subset once → to Google Drive**, then develop against that persistent index.

Use the notebook `AAOS_Agent_Colab.ipynb`. It:
1. mounts Drive (index persists there),
2. shallow-clones automotive-relevant projects (not full AOSP),
3. indexes that subset to `stores_root` on Drive (skips if already built),
4. starts **Ollama** with a coding model (e.g. `qwen2.5-coder:32b` or a lighter 14B)
   and runs the agent.

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
       + horizontal skill injection + VERDICT consensus
  → finalize (render diagnosis, grounded patch, UT ideas, human-review)
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
- **Fuse**: RRF → **min-max normalize to [0,1]** → additive customer/OEM + layer priors →
  cross-encoder rerank with **sigmoid-normalized** CE scores blended against the
  prior-boosted base — so the customer-first boost survives rerank
- **HIDL (two hard gates)**:
  1. Index time: `chunker.is_hidl` → `should_index = False` (path markers, `.hal`, `hidl_interface`)
  2. Retrieval time: hard **drop** any slipped `hidl_legacy` / HIDL path hit unless the
     query is explicitly about HIDL / migration

### Index filter & incremental (`retrieval/chunker.py`, `indexer.py`)

- `should_index(path, mode)` — `base` mode drops prebuilts/test/external/generated/oversized;
  `customer` mode keeps almost everything (customer patches land anywhere).
- `--incremental` — `git diff` the tree's HEAD against the last indexed SHA and touch only
  changed/added/deleted files in both Chroma and BM25 (pure local diff; no fetching).
- Manifest records `embed_model`, `git_sha`, and `index_roots`/`scope`.

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
