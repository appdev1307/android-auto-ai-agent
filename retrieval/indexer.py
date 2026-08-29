"""Build a per-tenant knowledge store (Chroma + BM25 + manifest).

Layout (physical isolation, option B):
    <stores_root>/_base/<aosp_version>/           ← shared AOSP base
    <stores_root>/<customer>/<project>/<aosp_version>/   ← isolated customer overlay

Examples:
    # base AOSP layer (shared, built once):
    python -m retrieval.indexer --aosp-root /aosp --base --aosp-version aosp15
    # a customer overlay:
    python -m retrieval.indexer --aosp-root /vinfast/tree \
        --customer vinfast --project vf8 --aosp-version aosp15
"""

from __future__ import annotations
import argparse
import json
import pickle
import subprocess
from pathlib import Path

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from tqdm import tqdm
import yaml

from retrieval.chunker import chunk_file, iter_files, should_index
from retrieval.hybrid import _tokenize
from retrieval.store import ChromaVectorStore, StoreManifest


def load_config(path: str = "data/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _git_sha(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _git_changed(root: Path, old_sha: str, new_sha: str) -> tuple[list[Path], list[Path]] | None:
    """Return (changed_or_added, deleted) file paths between two SHAs.

    Uses `git diff --name-status old new`. Returns None if git can't answer
    (shallow clone without the old commit, not a repo, etc.) — caller then
    falls back to a full rebuild.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-status", old_sha, new_sha],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
    except Exception:
        return None
    changed, deleted = [], []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]  # A/M/D/R...
        # For renames (Rxxx) git prints old\tnew — take the new path (last col).
        rel = parts[-1]
        p = (root / rel)
        if status == "D":
            deleted.append(p)
        else:
            changed.append(p)
    return changed, deleted


def _ids_for_path(path: str) -> str:
    """Chunk ids are '<path>::<idx>'. We delete by matching this path prefix."""
    return f"{path}::"


def _resolve_store_dir(rag: dict, *, base: bool, customer: str | None,
                       project: str, aosp_version: str) -> Path:
    root = Path(rag.get("stores_root", "indexes/stores"))
    if base:
        return root / "_base" / aosp_version
    if not customer:
        raise SystemExit("Need --base OR --customer to locate a store.")
    return root / customer / project / aosp_version


def build_index(aosp_root: str, config_path: str = "data/config.yaml", *,
                base: bool = False, customer: str | None = None,
                project: str = "default", aosp_version: str = "aosp15",
                reset: bool = False, incremental: bool = False,
                since_upstream: str | None = None):
    cfg = load_config(config_path)
    rag = cfg["rag"]
    root = Path(aosp_root).resolve()
    if not root.exists():
        raise SystemExit(f"AOSP root not found: {root}")

    # Customer tier keeps OEM patches that live in test/external/generated;
    # base tier filters aggressively.
    mode = "base" if base else "customer"

    store_dir = _resolve_store_dir(rag, base=base, customer=customer,
                                   project=project, aosp_version=aosp_version)
    chroma_dir = store_dir / "chroma"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    tag = "_base" if base else f"{customer}/{project}"
    print(f"[store] {tag}/{aosp_version} -> {store_dir}")

    client = chromadb.PersistentClient(
        path=str(chroma_dir), settings=Settings(anonymized_telemetry=False))
    if reset:
        try:
            client.delete_collection(ChromaVectorStore.COLLECTION)
        except Exception:
            pass
    collection = client.get_or_create_collection(
        name=ChromaVectorStore.COLLECTION, metadata={"hnsw:space": "cosine"})

    print(f"Loading embed model: {rag['embed_model']}")
    embedder = SentenceTransformer(rag["embed_model"])

    # ---- helpers shared by full + incremental ----------------------
    def chunk_files(paths: list[Path]) -> tuple[list[str], list[str], list[dict], list[dict]]:
        ids, docs, metas, bm = [], [], [], []
        for path in tqdm(paths, desc="chunk"):
            for ch in chunk_file(path, rag.get("chunk_size", 1200), rag.get("chunk_overlap", 200)):
                ids.append(ch["id"]); docs.append(ch["content"])
                metas.append({"path": ch["path"], "ext": ch["ext"], "layer": ch["layer"]})
                bm.append({"path": ch["path"], "content": ch["content"],
                           "layer": ch["layer"], "tokens": _tokenize(ch["content"])})
        return ids, docs, metas, bm

    def upsert(ids, docs, metas, batch=64):
        n = 0
        for i in range(0, len(ids), batch):
            sl = slice(i, i + batch)
            vecs = embedder.encode(docs[sl], show_progress_bar=False).tolist()
            collection.upsert(ids=ids[sl], documents=docs[sl],
                              metadatas=metas[sl], embeddings=vecs)
            n += len(ids[sl])
        return n

    # Files the OEM actually patched vs upstream — indexed UNCONDITIONALLY,
    # bypassing the filter (an OEM edit in test/ or external/ still matters).
    def oem_patched_files() -> list[Path]:
        if not since_upstream:
            return []
        diff = _git_changed(root, since_upstream, new_sha or "HEAD")
        if diff is None:
            print(f"[since-upstream] cannot diff against '{since_upstream}' "
                  f"(missing ref / shallow clone) — skipping OEM-patch union.")
            return []
        changed, _deleted = diff
        # keep only files that still exist + are real source (ext + not binary),
        # but SKIP the tier filter — that's the whole point.
        out = []
        for p in changed:
            if p.exists() and p.suffix.lower() in CODE_EXTS and not is_hidl(str(p)):
                try:
                    if p.stat().st_size <= MAX_INDEX_FILE_BYTES:
                        out.append(p)
                except OSError:
                    pass
        print(f"[since-upstream] {len(out)} OEM-patched files force-indexed "
              f"(vs {since_upstream})")
        return out

    bm25_path = store_dir / "bm25_corpus.pkl"
    prev = StoreManifest.load(store_dir / "manifest.json")
    new_sha = _git_sha(root)

    # ---- decide mode ----------------------------------------------
    do_incremental = (incremental and not reset and prev is not None
                      and prev.git_sha and new_sha)
    diff = None
    if do_incremental:
        diff = _git_changed(root, prev.git_sha, new_sha)
        if diff is None:
            print("[incremental] git diff unavailable (shallow clone?) — full rebuild.")
            do_incremental = False

    if do_incremental:
        changed, deleted = diff
        # re-apply the tier filter, but always keep OEM-patched files
        forced = {str(p.resolve()) for p in oem_patched_files()}
        changed = [p for p in changed
                   if should_index(p, mode=mode) or str(p.resolve()) in forced]
        print(f"[incremental] {prev.git_sha[:8]}→{new_sha[:8]}  "
              f"changed={len(changed)} deleted={len(deleted)}")

        # 1) drop old chunks of every touched file (changed OR deleted) from both stores
        touched = {str(p.resolve()) for p in (changed + deleted)}
        touched |= {str(p) for p in (changed + deleted)}
        for p in (changed + deleted):
            try:
                collection.delete(where={"path": str(p)})
                collection.delete(where={"path": str(p.resolve())})
            except Exception:
                pass
        corpus = []
        if bm25_path.exists():
            with open(bm25_path, "rb") as f:
                corpus = pickle.load(f)
        corpus = [c for c in corpus if c["path"] not in touched]

        # 2) re-chunk + upsert the changed files
        ids, docs, metas, bm = chunk_files(changed)
        added = upsert(ids, docs, metas)
        corpus.extend(bm)
        with open(bm25_path, "wb") as f:
            pickle.dump(corpus, f, protocol=pickle.HIGHEST_PROTOCOL)
        total_chunks = len(corpus)
        print(f"[incremental] +{added} chunks, corpus now {total_chunks}")

    else:
        # ---- full build -------------------------------------------
        index_roots = rag.get("index_roots") or ["vendor", "device", "packages/services/Car"]
        files: list[Path] = []
        for rel in index_roots:
            b = root / rel
            if b.exists():
                files.extend(list(iter_files(b, mode=mode)))
            else:
                print(f"[skip missing] {b}")
        if not files:
            files = list(iter_files(root, mode=mode))
        # UNION in OEM-patched files that the filter would otherwise drop
        files.extend(oem_patched_files())
        uniq, seen = [], set()
        for f in files:
            k = str(f.resolve())
            if k not in seen:
                seen.add(k); uniq.append(f)
        files = uniq
        print(f"Indexing {len(files)} files (mode={mode})")
        ids, docs, metas, bm = chunk_files(files)
        total_chunks = upsert(ids, docs, metas)
        with open(bm25_path, "wb") as f:
            pickle.dump(bm, f, protocol=pickle.HIGHEST_PROTOCOL)

    # Manifest — records the SHA we indexed, so next run can diff against it.
    manifest = StoreManifest(embed_model=rag["embed_model"], count=total_chunks, git_sha=new_sha)
    (store_dir / "manifest.json").write_text(manifest.to_json())
    print(f"Done. chunks={total_chunks}  store={store_dir}  git_sha={new_sha}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aosp-root", required=True)
    ap.add_argument("--config", default="data/config.yaml")
    ap.add_argument("--base", action="store_true", help="build the shared AOSP base layer")
    ap.add_argument("--customer", default=None)
    ap.add_argument("--project", default="default")
    ap.add_argument("--aosp-version", default="aosp15")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="only re-index files changed since the last indexed git SHA")
    ap.add_argument("--since-upstream", default=None, metavar="TAG_OR_SHA",
                    help="force-index every file the OEM patched vs this upstream ref "
                         "(e.g. android-15.0.0_r1), bypassing the tier filter")
    args = ap.parse_args()
    build_index(args.aosp_root, args.config, base=args.base, customer=args.customer,
                project=args.project, aosp_version=args.aosp_version,
                reset=args.reset, incremental=args.incremental,
                since_upstream=args.since_upstream)
