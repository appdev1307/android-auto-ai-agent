# AAOS / SDV Hybrid-RAG Agent (Option A)

**Real hybrid RAG** (vector + exact) + tool-calling agent for **Android 15 full stack**:

`HMI → CarService → AIDL → VHAL → VSS` with **customer/OEM components first** (`vendor/`, `device/`).

Not a prompt-only skeleton. Includes:
- Code-aware chunking + Chroma vector index
- Hybrid retrieve (semantic + ripgrep) with customer path boost
- Tools: `hybrid_search`, `read_source`, `lookup_vss_signal`, `find_aidl_interface`, `find_symbol`
- ReAct tool loop (LangGraph)
- Few-shot + system prompts for localize/patch
- vLLM OpenAI-compatible backend

Integration agent for A15 can plug in later; keep HAL/VSS contracts stable.

---

## 1. Install

```bash
cd android-auto-ai-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system: ripgrep (rg)
```

## 2. Build RAG index (customer-first roots)

```bash
export AOSP_ROOT=/path/to/your/tree
bash scripts/build_index.sh $AOSP_ROOT
# or: python -m retrieval.indexer --aosp-root $AOSP_ROOT --reset
```

Index roots (see `data/config.yaml`): `vendor` → `device` → apps → Car → automotive HAL → opt/car.

## 3. Start vLLM (A100 80GB)

```bash
bash scripts/start_vllm.sh
export OPENAI_API_BASE=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=dummy
```

## 4. Run agent

```bash
python -m agent.main --aosp-root $AOSP_ROOT \
  --bug "Android 15: VSS Vehicle.Speed not updating in HMI after ignition"
```

---

## Architecture

```
Bug/logcat
  → init HybridRetriever (Chroma + rg)
  → agent (LLM + tools)  ⇄  tool_node   # ReAct
  → finalize (ranked files, root cause, unified diff)
```

### RAG
- **Vector**: sentence-transformers embeddings over structural chunks
- **Sparse**: BM25 over the same chunks
- **Exact**: ripgrep on same roots
- **Fuse**: RRF (rank-based) → **min-max normalize to [0,1]** → additive customer/OEM + layer priors → cross-encoder rerank with **sigmoid-normalized** CE scores blended against the prior-boosted base (so the customer-first boost survives rerank instead of being wiped by raw CE logits).

### Tools
| Tool | Role |
|------|------|
| hybrid_search | vector + exact |
| read_source | truncated file read |
| lookup_vss_signal | VSS/catalog/mapping |
| find_aidl_interface | AIDL |
| find_symbol | symbol-oriented retrieve |

---

## Config

`data/config.yaml` — model endpoint, index roots, embed model, top_k, customer boost list.

Swap embed model to a code embedding model when you can afford it.

---

## What this is / is not

| Is | Is not |
|----|--------|
| Hybrid RAG + tools + ReAct | Full code-knowledge-graph (option B) |
| Customer-first ranking | Auto-apply to production |
| Full-stack path coverage | Perfect SWE-bench agent |

---

## Later: A15 integration agent

Leave property/AIDL/VSS contracts explicit in patches. Hook additional tools under `agent/tools_def.py` when you ship the integration agent.


## Multi-tenant knowledge (option B — physical IP isolation)

Each customer/OEM gets a physically separate store; base AOSP is shared read-only.

```
indexes/stores/
  _base/aosp15/                     # shared AOSP, built once
  vinfast/vf8/aosp15/               # isolated customer overlay
  bosch/projX/aosp15/
```

### Indexing any Android source (one command per tree)

Fresh AOSP from Google → `--base` (aggressive filter: drops prebuilts/test/external).
OEM tree from your org's git → `--customer <oem> --project <p>` (keeps almost
everything, since it's all patches/IP worth indexing); **add** `--since-upstream <tag>`
when that tree is a fork of AOSP sharing git history — it pulls in the exact files
the OEM patched inside `frameworks/base` that the normal filter would miss. If the
OEM tree is a detached snapshot with no history back to AOSP, drop `--since-upstream`
(the `--customer` mode already keeps everything). Both write the git SHA to the
manifest, so next time just add `--incremental` to re-index only the few thousand
changed files instead of the whole tree.

```bash
# fresh AOSP (shared base, once)
python -m retrieval.indexer --aosp-root /aosp --base --aosp-version aosp15

# OEM tree that forks AOSP
python -m retrieval.indexer --aosp-root /vinfast/tree \
    --customer vinfast --project vf8 --since-upstream android-15.0.0_r1

# OEM snapshot (no upstream history)
python -m retrieval.indexer --aosp-root /vinfast/tree --customer vinfast --project vf8

# later, after a sync — only changed files
python -m retrieval.indexer --aosp-root /vinfast/tree --customer vinfast --project vf8 --incremental
```

Run (tenant is explicit — never auto-picked):
```bash
python -m agent.main --bug "..." --customer vinfast --project vf8 --aosp-version aosp15
# omit --customer => base-only knowledge
```

Isolation is by construction: a session only loads `_base` + one customer store,
so another customer's code is never in the process. See `retrieval/store.py`.
