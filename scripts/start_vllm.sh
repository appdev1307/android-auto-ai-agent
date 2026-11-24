#!/usr/bin/env bash
# OpenAI-compatible vLLM server for the agent (server / workstation).
#
# - Tool calling is REQUIRED: agent/nodes.py binds tools (hybrid_search, read_source, ...).
#   Without --enable-auto-tool-choice vLLM rejects tool requests, and without the Qwen2.5
#   parser (hermes) tool calls come back as plain text.
# - Default weights are the 4-bit AWQ build: bf16 32B (~65 GB) leaves too little KV cache
#   for a 32k context even on an 80 GB GPU. AWQ is auto-detected from the checkpoint.
# - SERVED_NAME is what clients send as "model"; it must equal data/config.yaml model.name,
#   whichever checkpoint is loaded.
# - MAX_LEN: the prompt alone is ~9-10k tokens and max_tokens is 8192.
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-Coder-32B-Instruct-AWQ}"
SERVED_NAME="${SERVED_NAME:-Qwen/Qwen2.5-Coder-32B-Instruct}"
HOST="${HOST:-127.0.0.1}"          # set 0.0.0.0 only if other machines must reach it
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-32768}"
GPU_UTIL="${GPU_UTIL:-0.90}"
TOOL_PARSER="${TOOL_PARSER:-hermes}"
QUANT="${QUANT:-}"                 # empty = auto-detect from the checkpoint

# Avoids the FlashInfer sampler JIT (ninja) crash seen on Colab-style environments.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

ARGS=(serve "$MODEL"
      --served-model-name "$SERVED_NAME"
      --host "$HOST" --port "$PORT"
      --max-model-len "$MAX_LEN"
      --gpu-memory-utilization "$GPU_UTIL"
      --enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER")
if [ -n "$QUANT" ]; then ARGS+=(--quantization "$QUANT"); fi

echo "vLLM model=$MODEL served_as=$SERVED_NAME ${HOST}:${PORT} max_len=$MAX_LEN parser=$TOOL_PARSER"
exec vllm "${ARGS[@]}"
