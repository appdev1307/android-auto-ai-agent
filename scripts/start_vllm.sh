#!/usr/bin/env bash
set -e
# Defaults match data/config.yaml (model.name). The agent prompt alone is ~10k
# tokens and max_tokens is 8192, so MAX_LEN must be well above 8192.
# Smaller GPU (e.g. A100 40GB): MODEL=Qwen/Qwen2.5-Coder-32B-Instruct-AWQ QUANT=awq
# (and set model.name in config to the same id).
MODEL="${MODEL:-Qwen/Qwen2.5-Coder-32B-Instruct}"
PORT="${PORT:-8000}"
QUANT="${QUANT:-none}"
MAX_LEN="${MAX_LEN:-32768}"
echo "vLLM model=$MODEL port=$PORT quant=$QUANT max_len=$MAX_LEN"
ARGS=(--model "$MODEL" --host 0.0.0.0 --port "$PORT" --max-model-len "$MAX_LEN" --trust-remote-code)
if [ "$QUANT" != "none" ]; then ARGS+=(--quantization "$QUANT"); fi
exec python -m vllm.entrypoints.openai.api_server "${ARGS[@]}"
