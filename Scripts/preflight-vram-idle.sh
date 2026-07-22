#!/usr/bin/env bash
# preflight-vram-idle.sh — assert the GPU is near-idle BEFORE the retrieval
# group is first activated (i.e. before the wizard re-embed). Guards against a
# stale pinned Ollama 8B (~7GB) + retrieval group (~11.5-13GB) > 16,380 MiB OOM
# (§10 Step-0). Exits non-zero if used VRAM exceeds the threshold.
set -euo pipefail
THRESHOLD_MIB="${1:-1500}"   # near-idle ceiling; desktop/compositor headroom
USED="$(nvidia-smi --id=0 --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')"
echo "[preflight] GPU0 used = ${USED} MiB (threshold ${THRESHOLD_MIB} MiB)"
if [ "${USED}" -gt "${THRESHOLD_MIB}" ]; then
  echo "[preflight] FAIL: GPU not idle — retire the pinned pair first" >&2
  echo "  systemctl disable --now vllm-reranker.service" >&2
  echo "  ollama stop qwen3-embedding:8b   # or: systemctl stop ollama" >&2
  nvidia-smi >&2
  exit 1
fi
echo "[preflight] OK — GPU near-idle; safe to activate the retrieval group"
