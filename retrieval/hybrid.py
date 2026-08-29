"""
Hybrid retrieval for AAOS/SDV source code:
  dense (embedding) + BM25 (sparse) + exact (rg)
  → RRF fusion → code/path priors → cross-encoder rerank
"""

from __future__ import annotations
import os
import re
import math
import json
import pickle
import subprocess
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer, CrossEncoder
import yaml

from retrieval.chunker import guess_layer
from retrieval.store import (
    Tenant, VectorStore, ChromaVectorStore, StoreProvider, StoreManifest,
)


def _sigmoid(x: float) -> float:
    # squash unbounded cross-encoder logits into [0,1] so they blend fairly
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def load_config(path: str = "data/config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path(__file__).resolve().parents[1] / "data" / "config.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def _tokenize(text: str) -> list[str]:
    # Code-friendly tokens: identifiers + words
    return re.findall(r"[A-Za-z_][A-Za-z0-9_\.]+|[A-Za-z]{2,}", text.lower())


class HybridRetriever:
    def __init__(self, aosp_root: str | None = None, config_path: str = "data/config.yaml",
                 tenant: "Tenant | None" = None, store: "VectorStore | None" = None):
        self.cfg = load_config(config_path)
        self.rag = self.cfg.get("rag", {})
        self.rank_cfg = self.rag.get("ranking", {})
        self.tenant = tenant
        self.aosp_root = Path(
            aosp_root
            or self.cfg.get("retrieval", {}).get("aosp_root")
            or os.environ.get("AOSP_ROOT")
            or "."
        )
        self.embedder = None
        self.store: VectorStore | None = store
        self.cross_encoder: CrossEncoder | None = None

        if self.rag.get("enabled", True):
            self._init_embedder()
            if self.store is None:
                self._init_store()
            self._init_cross_encoder()

    # ------------------------------------------------------------------ init
    def _init_embedder(self):
        try:
            self.embedder = SentenceTransformer(self.rag["embed_model"])
        except Exception:
            self.embedder = None

    def _init_store(self):
        """Build the store. Multi-tenant when a tenant + stores.root are set;
        otherwise wrap the legacy single index_dir as one 'base' store."""
        embed_model = self.rag.get("embed_model", "")
        stores_root = self.rag.get("stores_root")
        if self.tenant is not None and stores_root:
            provider = StoreProvider(stores_root, embed_model)
            self.store = provider.open(self.tenant)
            return
        # Backward-compat: single flat index_dir → one store named 'base'.
        legacy_dir = Path(self.rag.get("index_dir", "indexes/chroma_aaos"))
        if legacy_dir.exists():
            self.store = _LegacyChromaStore(legacy_dir, self.rag, embed_model)

    def _init_cross_encoder(self):
        if not self.rank_cfg.get("cross_encoder_enabled", True):
            return
        model_name = self.rank_cfg.get(
            "cross_encoder_model", "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        try:
            self.cross_encoder = CrossEncoder(model_name)
        except Exception:
            self.cross_encoder = None

    # ------------------------------------------------------------------ public
    def retrieve(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        top_k = top_k or self.rag.get("top_k_final", 12)
        k_dense = self.rag.get("top_k_vector", 20)
        k_bm25 = self.rag.get("top_k_bm25", 20)
        k_exact = self.rag.get("top_k_exact", 20)
        k_fuse = self.rank_cfg.get("rrf_pool", 30)  # pool before rerank

        dense = self._vector_search(query, k_dense)
        bm25 = self._bm25_search(query, k_bm25)
        exact = self._exact_search(query, k_exact)

        fused = self._rrf_fuse(
            {"dense": dense, "bm25": bm25, "exact": exact},
            k=self.rank_cfg.get("rrf_k", 60),
        )
        # Normalize RRF base to [0,1] BEFORE priors, so the customer/OEM
        # boost is on the same scale as the fusion signal (else it either
        # dominates or gets wiped downstream).
        fused = self._normalize_base(fused)
        fused = self._apply_code_priors(fused, query)
        fused = sorted(fused, key=lambda x: x["score"], reverse=True)[:k_fuse]

        if self.cross_encoder and fused:
            fused = self._cross_encoder_rerank(query, fused, top_k=top_k)
        else:
            fused = fused[:top_k]
        return fused

    def _normalize_base(self, hits: list[dict]) -> list[dict]:
        norm = _minmax([h.get("score", 0.0) for h in hits])
        for h, n in zip(hits, norm):
            h["base_norm"] = n
            h["score"] = n  # priors will add on top of the [0,1] base
        return hits

    # ------------------------------------------------------------------ channels
    def _vector_search(self, query: str, k: int) -> list[dict]:
        if not self.store or not self.embedder:
            return []
        emb = self.embedder.encode([query]).tolist()[0]
        hits = self.store.query(emb, k)
        for i, h in enumerate(hits):
            h["source"] = "dense"
            h["rank"] = i + 1
        return hits

    def _bm25_search(self, query: str, k: int) -> list[dict]:
        if not self.store:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        hits = self.store.keyword(tokens, k)
        # store returns them BM25-sorted already; assign rank in order
        for rank, h in enumerate(hits, 1):
            h["source"] = "bm25"
            h["rank"] = rank
        return hits

    def _exact_search(self, query: str, k: int) -> list[dict]:
        keywords = self._keywords(query)
        if not keywords or not self.aosp_root.exists():
            return []
        pattern = "|".join(re.escape(w) for w in keywords[:8])
        roots = self.rag.get("index_roots") or ["."]
        hits = []
        rank = 0
        for rel in roots:
            base = self.aosp_root / rel
            if not base.exists():
                continue
            try:
                cmd = [
                    "rg", "-l", "-i", pattern, str(base),
                    "-g", "!out/", "-g", "!.git/", "-g", "!prebuilts/",
                    "-g", "*.java", "-g", "*.kt", "-g", "*.cpp", "-g", "*.c",
                    "-g", "*.h", "-g", "*.aidl", "-g", "*.yaml", "-g", "*.yml",
                    "-g", "*.json", "-g", "*.xml",
                ]
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
                for line in out.stdout.splitlines():
                    path = line.strip()
                    if not path:
                        continue
                    rank += 1
                    hits.append({
                        "path": path,
                        "content": self.read_file(path, max_chars=2000),
                        "layer": guess_layer(path),
                        "score": 1.0 / rank,  # rank signal for RRF
                        "source": "exact",
                        "rank": rank,
                    })
                    if rank >= k:
                        return hits
            except Exception:
                continue
        return hits[:k]

    # ------------------------------------------------------------------ RRF + priors + CE
    def _rrf_fuse(self, channels: dict[str, list[dict]], k: int = 60) -> list[dict]:
        """Reciprocal Rank Fusion: score += 1 / (k + rank)."""
        by_path: dict[str, dict] = {}
        for name, hits in channels.items():
            for h in hits:
                p = h.get("path") or ""
                if not p:
                    continue
                rank = h.get("rank") or 999
                rrf = 1.0 / (k + rank)
                if p not in by_path:
                    by_path[p] = {
                        "path": p,
                        "content": h.get("content", ""),
                        "layer": h.get("layer", "other"),
                        "score": 0.0,
                        "source": name,
                        "rrf_parts": {},
                    }
                by_path[p]["score"] += rrf
                by_path[p]["rrf_parts"][name] = rrf
                # keep longer snippet
                if len(h.get("content") or "") > len(by_path[p].get("content") or ""):
                    by_path[p]["content"] = h["content"]
                    by_path[p]["layer"] = h.get("layer", by_path[p]["layer"])
                src = set(by_path[p]["source"].split("+")) | {name}
                by_path[p]["source"] = "+".join(sorted(src))
        return list(by_path.values())

    def _apply_code_priors(self, hits: list[dict], query: str) -> list[dict]:
        """AAOS/SDV-specific multiplicative/additive priors after RRF."""
        boosts = self.rag.get("customer_path_boost") or ["vendor/", "device/"]
        q = query.lower()
        q_tokens = set(_tokenize(query))

        for h in hits:
            pl = h["path"].replace("\\", "/").lower()
            prior = 0.0

            # Customer / OEM first
            if any(b in pl for b in boosts):
                prior += self.rank_cfg.get("prior_customer", 0.12)
            # Store-level customer-first: hits from the isolated customer store
            # get a boost even if their path doesn't match the patterns above.
            if h.get("store") == "customer":
                prior += self.rank_cfg.get("prior_customer_store", 0.06)

            # Layer priors from query intent
            if any(x in q for x in ("vss", "signal", "vehicle.speed", "covesa")):
                if "vss" in pl or "signal" in pl or pl.endswith((".yaml", ".yml")):
                    prior += 0.08
            if "aidl" in q or "interface" in q:
                if pl.endswith(".aidl") or "/aidl" in pl:
                    prior += 0.08
            if any(x in q for x in ("vhal", "hal", "vehiclehal", "property")):
                if "automotive" in pl or "vehicle" in pl or pl.endswith((".cpp", ".h")):
                    prior += 0.06
            if any(x in q for x in ("carservice", "carproperty", "car service")):
                if "services/car" in pl or pl.endswith(".java") or pl.endswith(".kt"):
                    prior += 0.06
            if any(x in q for x in ("hmi", "ui", "compose", "activity", "fragment")):
                if "packages/apps" in pl or "carui" in pl or "hmi" in pl:
                    prior += 0.06

            # Exact identifier hit in path (CarPropertyService, IVehicle, ...)
            path_ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]+", Path(h["path"]).stem))
            if path_ids & q_tokens:
                prior += self.rank_cfg.get("prior_symbol_in_path", 0.1)

            # Extension prior for stack files
            ext = Path(h["path"]).suffix.lower()
            if ext in {".aidl", ".java", ".kt", ".cpp", ".h", ".yaml", ".yml"}:
                prior += 0.02

            # Legacy HIDL: down-weight hard unless the query is explicitly
            # about HIDL / migration. We keep these indexable (comparison,
            # porting) but they must never outrank AIDL for an A14+ question.
            if h.get("layer") == "hidl_legacy":
                if any(x in q for x in ("hidl", "migrat", "legacy", "v2_0", "2.0", ".hal")):
                    prior += 0.0
                else:
                    prior -= self.rank_cfg.get("prior_hidl_penalty", 0.30)

            h["score"] = h["score"] + prior
            h["prior"] = prior
        return hits

    def _cross_encoder_rerank(self, query: str, hits: list[dict], top_k: int) -> list[dict]:
        pairs = []
        for h in hits:
            snippet = (h.get("content") or "")[:1500]
            pairs.append([query, f"{h['path']}\n{snippet}"])
        try:
            ce_scores = self.cross_encoder.predict(pairs)
        except Exception:
            return hits[:top_k]
        alpha = self.rank_cfg.get("ce_blend", 0.75)
        for h, s in zip(hits, ce_scores):
            h["ce_score"] = float(s)
            # Both terms in [0,1] now: sigmoid(CE logit) vs. normalized
            # RRF+prior score. So `ce_blend` actually controls the mix and
            # the customer/OEM prior is no longer numerically wiped out.
            h["ce_norm"] = _sigmoid(float(s))
            h["score"] = alpha * h["ce_norm"] + (1 - alpha) * h["score"]
        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits[:top_k]

    # ------------------------------------------------------------------ utils
    def read_file(self, path: str, max_chars: int | None = None) -> str:
        max_chars = max_chars or self.cfg.get("retrieval", {}).get("max_file_chars", 14000)
        p = Path(path)
        if not p.is_absolute():
            p = self.aosp_root / path
        # Confine reads to the source tree — the LLM controls `path`, so an
        # absolute path like /etc/passwd must not be readable.
        try:
            rp = p.resolve()
            root = self.aosp_root.resolve()
            if root != Path(".").resolve() and root not in rp.parents and rp != root:
                return f"[refused: {path} is outside aosp_root]"
        except Exception:
            pass
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if len(text) > max_chars:
                return text[:max_chars] + "\n\n... [truncated] ..."
            return text
        except Exception as e:
            return f"[error reading {path}: {e}]"

    def lookup_vss(self, signal_hint: str) -> list[dict]:
        hits = self.retrieve(f"VSS signal {signal_hint} Vehicle catalog yaml", top_k=12)
        filt = [h for h in hits if h.get("layer") == "vss"
                or h["path"].lower().endswith((".yaml", ".yml", ".vss"))]
        return (filt or hits)[:8]

    def find_aidl(self, name_hint: str) -> list[dict]:
        hits = self.retrieve(f"AIDL interface {name_hint} .aidl parcelable", top_k=12)
        filt = [h for h in hits if h.get("layer") == "aidl"
                or h["path"].lower().endswith(".aidl")]
        return (filt or hits)[:8]

    def _keywords(self, query: str) -> list[str]:
        stop = {"the", "a", "an", "after", "on", "in", "of", "and", "or", "to", "for", "with", "not", "is"}
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", query)
        boost = [w for w in words if any(x in w.lower() for x in
                 ("hal", "vhal", "vss", "aidl", "car", "vehicle", "property", "signal", "speed", "hmi"))]
        base = [w for w in words if w.lower() not in stop]
        return boost or base[:10]


# ── Backward-compat: wrap the old flat index_dir as a single 'base' store ──
class _LegacyChromaStore:
    """Adapter over the pre-multitenant layout: <index_dir>/{chroma files, bm25_corpus.pkl}
    with collection_name from config. Lets existing indexes keep working."""

    name = "base"

    def __init__(self, index_dir: Path, rag: dict, embed_model: str):
        self.index_dir = Path(index_dir)
        self._collection = None
        self._bm25: BM25Okapi | None = None
        self._corpus: list[dict] = []
        try:
            client = chromadb.PersistentClient(
                path=str(self.index_dir),
                settings=Settings(anonymized_telemetry=False))
            self._collection = client.get_collection(rag.get("collection_name", "aaos_sdv_fullstack"))
        except Exception:
            self._collection = None
        bm25_path = self.index_dir / "bm25_corpus.pkl"
        if bm25_path.exists():
            try:
                with open(bm25_path, "rb") as f:
                    self._corpus = pickle.load(f)
                toks = [c.get("tokens") or _tokenize(c.get("content", "")) for c in self._corpus]
                if any(toks):
                    self._bm25 = BM25Okapi(toks)
            except Exception:
                self._bm25, self._corpus = None, []

    def query(self, embedding: list[float], k: int) -> list[dict]:
        if self._collection is None:
            return []
        res = self._collection.query(
            query_embeddings=[embedding], n_results=k,
            include=["documents", "metadatas", "distances"])
        ids = res.get("ids", [[]])[0]
        out = []
        for i, _ in enumerate(ids):
            meta = res["metadatas"][0][i] or {}
            dist = res["distances"][0][i] if res.get("distances") else 1.0
            out.append({
                "path": meta.get("path", ""),
                "content": res["documents"][0][i],
                "layer": meta.get("layer", "other"),
                "score": max(0.0, min(1.0, 1.0 - float(dist))),
                "store": self.name,
            })
        return out

    def keyword(self, tokens: list[str], k: int) -> list[dict]:
        if not self._bm25 or not self._corpus or not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        out = []
        for i in idx:
            if scores[i] <= 0:
                continue
            c = self._corpus[i]
            out.append({
                "path": c["path"], "content": c.get("content", ""),
                "layer": c.get("layer", "other"), "score": float(scores[i]),
                "store": self.name,
            })
        return out

    def manifest(self):
        return None
