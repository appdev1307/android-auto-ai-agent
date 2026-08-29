# AAOS / SDV Hybrid-RAG Agent

**Real hybrid RAG** (dense + BM25 + exact) + tool-calling ReAct agent for the
**Android 15 full stack**:

`HMI → CarService → AIDL → VHAL → VSS`, ranked **customer/OEM-first** (`vendor/`, `device/`),
with **physical IP isolation per customer** (multi-tenant, option B).

Not a prompt-only skeleton. Includes:
- Structural chunking + Chroma vector index + BM25 corpus
- Hybrid retrieve (dense + BM25 + ripgrep) → RRF → normalize → customer/OEM priors → cross-encoder rerank
- Legacy **HIDL excluded**, tagged `hidl_legacy` and down-weighted (A14+ wants AIDL)
- Aggressive index filter (drops prebuilts/test/external/generated) + **incremental re-index** by git SHA
- Multi-tenant stores: shared AOSP base + per-customer isolated overlay
- Tools: `hybrid_search`, `read_source`, `lookup_vss_signal`, `find_aidl_interface`, `find_symbol`
- ReAct tool loop (LangGraph) + forced human-review on VHAL/VSS/AIDL/SELinux/power
- vLLM OpenAI-compatible backend

---

## 1. Install

```bash
cd android-auto-ai-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system dependency: ripgrep (rg)
```

## 2. Index a source tree (one command per tree)

**You fetch the source, the tool never does.** Clone/`repo sync`/export the tree yourself
(fresh AOSP, or a customer-specific tree from your org's git) however your workflow does it,
then point `--aosp-root` at it. The indexer only reads that path and indexes it; the agent
uses the resulting store.

Fresh AOSP → `--base` (aggressive filter: drops prebuilts/test/external). Customer tree →
`--customer <oem> --project <p>` (keeps almost everything, since it's all patches/IP worth
indexing). Both write the git SHA to the store manifest, so after you re-sync a tree, add
`--incremental` to re-index only the changed files instead of the whole tree.

```bash
# fresh AOSP (shared base, built once)
python -m retrieval.indexer --aosp-root /aosp --base --aosp-version aosp15

# a customer tree you fetched
python -m retrieval.indexer --aosp-root /oem/tree --customer oem-a --project proj1

# later, after you re-sync that tree — only changed files
python -m retrieval.indexer --aosp-root /oem/tree --customer oem-a --project proj1 --incremental
```

## 3. Start vLLM

```bash
bash scripts/start_vllm.sh
export OPENAI_API_BASE=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=dummy
```

## 4. Run the agent

```bash
# base-only knowledge
python -m agent.main --aosp-root /aosp \
  --bug "Android 15: VSS Vehicle.Speed not updating in HMI after ignition ON"

# with a customer overlay (tenant is explicit — never auto-picked)
python -m agent.main --bug "..." --customer oem-a --project proj1 --aosp-version aosp15
```

---

## Architecture

```
Bug/logcat
  → init_retriever  (base ∪ customer stores: Chroma + BM25 + rg)
  → agent (LLM + tools)  ⇄  tools (ToolNode)     # ReAct loop
  → finalize (ranked files, root cause, unified diff, human-review flag)
```

Four layers wired through `AgentState`: **graph** (LangGraph orchestration), **tools**
(`agent/tools_def.py`), **retrieval** (`retrieval/hybrid.py` + `store.py`), **safety**
(`finalize`). See `CHANGES.md` for the full change history.

### Retrieval pipeline (`retrieval/hybrid.py`)

- **Dense**: sentence-transformers embeddings over structural chunks (Chroma, cosine)
- **Sparse**: BM25 over the same chunks
- **Exact**: ripgrep on the same roots (catches rare symbols embeddings miss)
- **Fuse**: RRF (rank-based, scale-free) → **min-max normalize to [0,1]** → additive
  customer/OEM + layer priors → cross-encoder rerank with **sigmoid-normalized** CE scores
  blended against the prior-boosted base — so the customer-first boost survives rerank
  instead of being wiped by raw CE logits.
- **HIDL**: excluded at index time by path (+ `.hal` / `hidl_interface` signals); any that
  slip through are tagged `hidl_legacy` and penalized unless the query is about HIDL/migration.

### Index filter & incremental (`retrieval/chunker.py`, `indexer.py`)

- `should_index(path, mode)` — `base` mode drops prebuilts/test/external/generated/oversized;
  `customer` mode keeps almost everything (customer patches land anywhere).
- `--incremental` — `git diff` the tree's HEAD against the last indexed SHA and touch only
  changed/added/deleted files in both Chroma and BM25 (pure local diff; no fetching).

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

A session only ever loads `_base` + **one** customer store, so another customer's code is
never in the process — isolation is by construction, not "trust the filter". Built with five
patterns in `retrieval/store.py`: Repository (`VectorStore`), Adapter (`ChromaVectorStore`),
Factory (`StoreProvider` — the one place tenant→path resolves), Composite (`CompositeStore`),
Facade (`KnowledgeSession`). Guards fail closed: at most one customer layer, path sandboxed
to root, embed-model mismatch raises.

---

## Config

`data/config.yaml` — model endpoint, `stores_root`, index roots, embed model, ranking weights
(`ce_blend`, `prior_customer`, `prior_customer_store`, `prior_hidl_penalty`), `max_tool_iters`,
safety (`auto_apply: false`). Swap the embed model to a code-embedding model when you can afford it.