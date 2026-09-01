"""Structured tools for the agent — hybrid RAG backed."""

from __future__ import annotations
from typing import Any
from langchain_core.tools import tool

# Retriever injected at runtime
_retriever = None


def set_retriever(r):
    global _retriever
    _retriever = r


def get_retriever():
    return _retriever


@tool
def hybrid_search(query: str) -> str:
    """Hybrid search (vector RAG + exact) over AAOS/SDV tree. Customer/OEM paths boosted.
    Use for bug localization across HMI, CarService, AIDL, VHAL, VSS."""
    if not _retriever:
        return "Retriever not initialized"
    hits = _retriever.retrieve(query)
    lines = []
    for i, h in enumerate(hits, 1):
        snippet = (h.get("content") or "")[:500].replace("\n", " ")
        prior = h.get("prior", 0.0) or 0.0
        why = f"score={h.get('score', 0):.3f}"
        if prior:
            why += f" prior=+{prior:.2f}"          # customer/OEM + layer boost
        if h.get("ce_score") is not None:
            why += f" ce={h.get('ce_score'):.2f}"   # cross-encoder relevance
        lines.append(
            f"{i}. [{h.get('layer')}|{h.get('source')}|{why}] {h.get('path')}\n   {snippet}"
        )
    return "\n".join(lines) if lines else "No hits. Build index first: python -m retrieval.indexer --aosp-root $AOSP_ROOT"


@tool
def read_source(path: str) -> str:
    """Read a source file (truncated). Path from hybrid_search results."""
    if not _retriever:
        return "Retriever not initialized"
    return _retriever.read_file(path)


@tool
def lookup_vss_signal(signal_name: str) -> str:
    """Find VSS signal definitions and mapping (YAML/catalog/mapper code)."""
    if not _retriever:
        return "Retriever not initialized"
    hits = _retriever.lookup_vss(signal_name)
    return "\n".join(f"- {h['path']} ({h['layer']})\n{(h.get('content') or '')[:400]}" for h in hits) or "No VSS hits"


@tool
def find_aidl_interface(name: str) -> str:
    """Find AIDL interface definitions and related stubs."""
    if not _retriever:
        return "Retriever not initialized"
    hits = _retriever.find_aidl(name)
    return "\n".join(f"- {h['path']}\n{(h.get('content') or '')[:400]}" for h in hits) or "No AIDL hits"


@tool
def find_symbol(symbol: str) -> str:
    """Exact-ish search for class/method/property symbol across stack."""
    if not _retriever:
        return "Retriever not initialized"
    hits = _retriever.retrieve(symbol, top_k=10)
    return "\n".join(f"- {h['path']} [{h['layer']}]\n{(h.get('content') or '')[:300]}" for h in hits) or "No symbol hits"


ALL_TOOLS = [hybrid_search, read_source, lookup_vss_signal, find_aidl_interface, find_symbol]
