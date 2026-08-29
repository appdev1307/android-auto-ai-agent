"""Multi-tenant knowledge store — physical isolation per customer (option B).

Design patterns, each killing one concrete risk:
  - Repository  (VectorStore)      : retrievers depend on an interface, not on chromadb
  - Adapter     (ChromaVectorStore): all chroma-specific code in one class
  - Factory     (StoreProvider)    : the ONLY place tenant -> path is resolved (audit here)
  - Composite   (CompositeStore)   : base + customer behind one interface; the retriever
                                     is tenant-blind, so it cannot query the wrong customer
  - Facade      (KnowledgeSession) : tenant is pinned at open(); the agent sees one entry

Isolation is by construction: a session's CompositeStore only ever contains
[base, <one customer>]. Another customer's code is not in the object graph, so
there is no query path to it — this is provable at audit, not "trust the filter".

Layout on disk:
    <root>/_base/<aosp_version>/{chroma, bm25_corpus.pkl, manifest.json}
    <root>/<customer>/<project>/<aosp_version>/{chroma, bm25_corpus.pkl, manifest.json}
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import chromadb
from chromadb.config import Settings
from rank_bm25 import BM25Okapi


# ── Value objects ────────────────────────────────────────────────
@dataclass(frozen=True)
class Tenant:
    """Immutable address of a knowledge store. frozen=True → cannot drift mid-session."""
    customer: str
    project: str = "default"
    aosp_version: str = "aosp15"

    @property
    def slug(self) -> str:
        return f"{self.customer}/{self.project}/{self.aosp_version}"


@dataclass
class StoreManifest:
    embed_model: str
    count: int = 0
    git_sha: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def load(cls, path: Path) -> "StoreManifest | None":
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return cls(embed_model=d.get("embed_model", ""),
                   count=d.get("count", 0), git_sha=d.get("git_sha"))


# ── Repository interface ─────────────────────────────────────────
@runtime_checkable
class VectorStore(Protocol):
    name: str
    def query(self, embedding: list[float], k: int) -> list[dict[str, Any]]: ...
    def keyword(self, tokens: list[str], k: int) -> list[dict[str, Any]]: ...
    def manifest(self) -> StoreManifest | None: ...


# ── Adapter: one Chroma collection + one BM25 corpus at a dir ─────
class ChromaVectorStore:
    """Wraps a single on-disk store dir. The only class that knows chromadb."""

    COLLECTION = "chunks"

    def __init__(self, persist_dir: Path, name: str, expected_embed_model: str):
        self.name = name
        self.persist_dir = Path(persist_dir)
        self._collection = None
        self._bm25: BM25Okapi | None = None
        self._corpus: list[dict] = []
        self._manifest = StoreManifest.load(self.persist_dir / "manifest.json")

        # Guard: a store built with a different embed model would return
        # garbage/incompatible vectors. Fail loudly, don't silently mis-rank.
        if self._manifest and self._manifest.embed_model and expected_embed_model \
                and self._manifest.embed_model != expected_embed_model:
            raise ValueError(
                f"[store:{name}] embed model mismatch — index built with "
                f"'{self._manifest.embed_model}' but retriever uses "
                f"'{expected_embed_model}'. Re-index or fix config.")
        if self._manifest is None:
            print(f"[store:{name}] no manifest.json — skipping embed-model guard "
                  f"(legacy index; consider re-indexing to write one).")

        self._load()

    def _load(self) -> None:
        chroma_dir = self.persist_dir / "chroma"
        if chroma_dir.exists():
            try:
                client = chromadb.PersistentClient(
                    path=str(chroma_dir),
                    settings=Settings(anonymized_telemetry=False))
                self._collection = client.get_collection(self.COLLECTION)
            except Exception:
                self._collection = None
        bm25_path = self.persist_dir / "bm25_corpus.pkl"
        if bm25_path.exists():
            try:
                with open(bm25_path, "rb") as f:
                    self._corpus = pickle.load(f)
                toks = [c.get("tokens") or [] for c in self._corpus]
                if any(toks):
                    self._bm25 = BM25Okapi(toks)
            except Exception:
                self._bm25, self._corpus = None, []

    def query(self, embedding: list[float], k: int) -> list[dict[str, Any]]:
        if self._collection is None:
            return []
        res = self._collection.query(
            query_embeddings=[embedding], n_results=k,
            include=["documents", "metadatas", "distances"])
        ids = res.get("ids", [[]])[0]
        out = []
        for i, _ in enumerate(ids):
            meta = (res["metadatas"][0][i] or {})
            dist = res["distances"][0][i] if res.get("distances") else 1.0
            out.append({
                "path": meta.get("path", ""),
                "content": res["documents"][0][i],
                "layer": meta.get("layer", "other"),
                "score": max(0.0, min(1.0, 1.0 - float(dist))),
                "store": self.name,
            })
        return out

    def keyword(self, tokens: list[str], k: int) -> list[dict[str, Any]]:
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
                "path": c["path"],
                "content": c.get("content", ""),
                "layer": c.get("layer", "other"),
                "score": float(scores[i]),
                "store": self.name,
            })
        return out

    def manifest(self) -> StoreManifest | None:
        return self._manifest


# ── Composite: base ⊕ customer behind one VectorStore ────────────
class CompositeStore:
    """Fan a query across ordered layers and tag each hit with its store name.

    The retriever holds ONE of these and never sees the individual layers, so it
    cannot address a store that isn't in here. Isolation is structural.
    """

    def __init__(self, layers: list[VectorStore], name: str = "composite"):
        if not layers:
            raise ValueError("CompositeStore needs at least one layer")
        # Guard: at most one customer layer — a second customer in the same
        # composite would be a routing bug, and IP cross-leak. Refuse it.
        customer_layers = [l for l in layers if getattr(l, "name", "") == "customer"]
        if len(customer_layers) > 1:
            raise ValueError("CompositeStore must contain at most ONE customer layer")
        self.layers = layers
        self.name = name

    def query(self, embedding: list[float], k: int) -> list[dict[str, Any]]:
        hits: list[dict] = []
        for s in self.layers:
            hits += s.query(embedding, k)
        return hits

    def keyword(self, tokens: list[str], k: int) -> list[dict[str, Any]]:
        hits: list[dict] = []
        for s in self.layers:
            hits += s.keyword(tokens, k)
        return hits

    def manifest(self) -> StoreManifest | None:
        return self.layers[0].manifest()


# ── Factory: the ONE place tenant → path is resolved ─────────────
class StoreProvider:
    """Resolves a Tenant to a base + customer CompositeStore. Audit IP routing here."""

    def __init__(self, root: str | Path, embed_model: str):
        self.root = Path(root).resolve()
        self.embed_model = embed_model

    def _base_dir(self, aosp_version: str) -> Path:
        return self.root / "_base" / aosp_version

    def _customer_dir(self, t: Tenant) -> Path:
        return self.root / t.customer / t.project / t.aosp_version

    def _assert_under_root(self, p: Path) -> None:
        rp = p.resolve()
        if self.root != rp and self.root not in rp.parents:
            raise ValueError(f"[provider] resolved store path {rp} escapes root {self.root}")

    def open(self, t: Tenant) -> CompositeStore:
        layers: list[VectorStore] = []

        base_dir = self._base_dir(t.aosp_version)
        self._assert_under_root(base_dir)
        if base_dir.exists():
            layers.append(ChromaVectorStore(base_dir, "base", self.embed_model))

        cust_dir = self._customer_dir(t)
        self._assert_under_root(cust_dir)
        if cust_dir.exists():
            layers.append(ChromaVectorStore(cust_dir, "customer", self.embed_model))

        if not layers:
            raise FileNotFoundError(
                f"[provider] no store for tenant '{t.slug}'. Build one:\n"
                f"  python -m retrieval.indexer --aosp-root <path> "
                f"--customer {t.customer} --project {t.project} "
                f"--aosp-version {t.aosp_version}")
        return CompositeStore(layers, name=t.slug)


# ── Facade: pin the tenant, expose one entry point ───────────────
class KnowledgeSession:
    def __init__(self, tenant: Tenant, store: CompositeStore):
        self.tenant = tenant
        self.store = store

    @classmethod
    def open(cls, tenant: Tenant, provider: StoreProvider) -> "KnowledgeSession":
        return cls(tenant, provider.open(tenant))
