#!/usr/bin/env bash
set -e
MODEL="${MODEL:-meta-llama/Llama-3.1-70B-Instruct}"
PORT="${PORT:-8000}"
QUANT="${QUANT:-awq}"
MAX_LEN="${MAX_LEN:-8192}"
echo "vLLM model=$MODEL port=$PORT quant=$QUANT"
ARGS=(--model "$MODEL" --host 0.0.0.0 --port "$PORT" --max-model-len "$MAX_LEN" --trust-remote-code)
if [ "$QUANT" != "none" ]; then ARGS+=(--quantization "$QUANT"); fi
exec python -m vllm.entrypoints.openai.api_server "${ARGS[@]}"
