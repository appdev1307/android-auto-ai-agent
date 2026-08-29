#!/usr/bin/env bash
# Build hybrid RAG index — customer roots first (see data/config.yaml)
set -e
ROOT="${1:-$AOSP_ROOT}"
if [ -z "$ROOT" ]; then
  echo "Usage: $0 /path/to/aosp_or_sdv_tree"
  exit 1
fi
python -m retrieval.indexer --aosp-root "$ROOT" --reset
echo "Index ready under indexes/chroma_aaos"
