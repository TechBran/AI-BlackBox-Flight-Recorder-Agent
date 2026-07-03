#!/usr/bin/env bash
# blackbox-install-reranker — GPU-gated vLLM reranker provisioning (retrieval
# M13, Part A). Templated from the deployed MS-02 Ultra setup (the proven
# configuration): a dedicated pip venv at $REAL_HOME/rerank-venv, a
# ~/start-reranker.sh start script, and a vllm-reranker.service systemd unit
# serving Qwen/Qwen3-Reranker-0.6B on port 8091 — the port
# Orchestrator/rerank.py's base_url code fallback expects, so a fresh box
# needs ZERO url/port config edits once this service is up. Enablement stays
# a deliberate operator flip ([rerank] provider = vllm + [retrieval]
# rerank_enabled = true — see the rerank.py activation checklist); nothing
# here writes config.ini.
#
# GPU GATE: requires nvidia-smi + >=8000 MB VRAM. CPU boxes get a clear skip
# message and exit 0 — vLLM must never be installed on a CPU box.
#
# IDEMPOTENT / re-run safe:
#   - venv exists  -> upgrade path (pip install --upgrade vllm)
#   - unit exists  -> re-written + daemon-reload (same sed-template flow as
#                     install.sh's zellij-web.service step)
#   - service already running -> restarted, then re-verified
#
# Usage (install.sh Step 2d passes all three; standalone auto-detects):
#   sudo bash installer/templates/blackbox-install-reranker.sh \
#       [real_user] [real_home] [blackbox_root]
#
# Exit codes:
#   0 — installed + serving (or clean CPU-box skip)
#   2 — could not resolve user/home
#   4 — venv creation or vllm pip install failed
#   6 — service failed to start / never answered on port 8091 in time
#
# install.sh invokes this NON-FATALLY (like the Ollama step): retrieval works
# un-reranked, and the wizard's Memory & Search step shows the remediation.

set -euo pipefail

REAL_USER="${1:-}"
REAL_HOME="${2:-}"
BLACKBOX_ROOT="${3:-}"

# ── Resolve user/home/root (mirrors install.sh Step 0 for standalone runs) ──
if [[ -z "$REAL_USER" ]]; then
    if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then
        REAL_USER="$SUDO_USER"
    else
        REAL_USER="${USER:-}"
    fi
fi
if [[ -z "$REAL_HOME" && -n "$REAL_USER" ]]; then
    REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
fi
if [[ -z "$REAL_USER" || -z "$REAL_HOME" ]]; then
    echo "[install-reranker] ERROR: could not resolve user/home (got user='$REAL_USER' home='$REAL_HOME')" >&2
    exit 2
fi
if [[ -z "$BLACKBOX_ROOT" ]]; then
    # This script lives at $BLACKBOX_ROOT/installer/templates/ — two dirs up.
    BLACKBOX_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi

RERANK_PORT=8091   # MUST match Orchestrator/rerank.py DEFAULT_BASE_URL
MODEL_ID="Qwen/Qwen3-Reranker-0.6B"
VENV_DIR="$REAL_HOME/rerank-venv"
START_SCRIPT="$REAL_HOME/start-reranker.sh"
UNIT_TEMPLATE="$BLACKBOX_ROOT/installer/templates/vllm-reranker.service"
UNIT_DEST="/etc/systemd/system/vllm-reranker.service"
VERIFY_TIMEOUT_S=600   # matches the unit's TimeoutStartSec (first start
                       # downloads ~1.2 GB of weights from Hugging Face)

# ── GPU gate: nvidia-smi present + >=8000 MB VRAM ──────────────────────────
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[install-reranker] No NVIDIA GPU detected (nvidia-smi not found) — skipping."
    echo "[install-reranker] The cross-encoder reranker is a GPU feature (MS-02 Ultra);"
    echo "[install-reranker] search works fully without it on this box."
    exit 0
fi
# Largest GPU wins on a multi-GPU box; nounits keeps the value a bare MiB int.
VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
    | sort -nr | head -n1 | tr -d '[:space:]' || true)"
if ! [[ "$VRAM_MB" =~ ^[0-9]+$ ]]; then
    echo "[install-reranker] nvidia-smi present but VRAM unreadable ('$VRAM_MB') — skipping."
    echo "[install-reranker] (Driver not loaded? Check 'nvidia-smi' by hand, then re-run"
    echo "[install-reranker]  sudo bash installer/templates/blackbox-install-reranker.sh)"
    exit 0
fi
if (( VRAM_MB < 8000 )); then
    echo "[install-reranker] GPU has ${VRAM_MB} MB VRAM (< 8000 MB required) — skipping."
    echo "[install-reranker] The reranker shares the card with the Ollama embedder and"
    echo "[install-reranker] needs the headroom; search works fully without it."
    exit 0
fi
echo "[install-reranker] GPU gate passed (${VRAM_MB} MB VRAM) — provisioning the vLLM reranker."

# ── Rerank venv: create if missing, else upgrade path ──────────────────────
# python3.12 is a MUST_HAVE apt package (system-packages.txt) — prefer it to
# match the proven Ultra setup; fall back to python3 for forward-compat.
PYBIN="$(command -v python3.12 || command -v python3)"
if [[ ! -x "$VENV_DIR/bin/pip" ]]; then
    echo "[install-reranker] Creating venv at $VENV_DIR ($PYBIN)..."
    if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$VENV_DIR"; then
        echo "[install-reranker] ERROR: venv creation failed at $VENV_DIR" >&2
        echo "[install-reranker] (Is python3.12-venv installed? It's in the MUST_HAVE apt set.)" >&2
        exit 4
    fi
else
    echo "[install-reranker] venv already at $VENV_DIR — taking the upgrade path."
fi
echo "[install-reranker] Installing/upgrading vllm (large download on first install)..."
if ! sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install --upgrade vllm; then
    echo "[install-reranker] ERROR: pip install vllm failed" >&2
    echo "[install-reranker] (Check network/disk, then re-run" >&2
    echo "[install-reranker]  sudo bash installer/templates/blackbox-install-reranker.sh)" >&2
    exit 4
fi

# ── Start script (the proven MS-02 Ultra incantation, paths parameterized) ──
# --gpu-memory-utilization MUST stay in the 0.15-0.25 band: the Ollama
# embedder shares the card and an unconstrained vLLM pre-allocates ~90% of
# VRAM and evicts it (rerank.py checklist). The hf-overrides convert the
# published CausalLM weights to sequence classification — without them
# vLLM's /score endpoint does not exist.
TMP_START="$(mktemp)"
trap 'rm -f "$TMP_START"' EXIT
cat > "$TMP_START" <<EOF
#!/bin/bash
# Generated by blackbox-install-reranker.sh — DO NOT EDIT BY HAND.
# Serves the BlackBox M11 cross-encoder reranker (Orchestrator/rerank.py).
exec $VENV_DIR/bin/vllm serve $MODEL_ID \\
  --port $RERANK_PORT --gpu-memory-utilization 0.20 --max-model-len 8192 \\
  --hf-overrides '{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'
EOF
# Root-side `install` for atomic write + correct ownership in one call
# (mktemp files are 0600 — a sudo -u cp would not be able to read them).
sudo install -m 0755 -o "$REAL_USER" -g "$REAL_USER" "$TMP_START" "$START_SCRIPT"
echo "[install-reranker] Wrote $START_SCRIPT"

# ── Systemd unit: substitute placeholders, (re-)write, enable, restart ──────
# Same sed-template flow as install.sh's zellij-web.service step; re-writing
# an existing unit + daemon-reload is the idempotent path.
sed -e "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" \
    -e "s|REAL_HOME_PLACEHOLDER|$REAL_HOME|g" \
    "$UNIT_TEMPLATE" | sudo tee "$UNIT_DEST" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable vllm-reranker.service > /dev/null 2>&1
sudo systemctl restart vllm-reranker.service
echo "[install-reranker] vllm-reranker.service written + enabled + (re)started"

# ── Verify: poll the vLLM model-list endpoint until it answers ──────────────
# First boot downloads the model weights before the port binds — poll with a
# long ceiling and keep the operator informed. /v1/models answers 200 as soon
# as the engine is up (same surface Orchestrator/rerank.py probes).
echo "[install-reranker] Waiting for http://127.0.0.1:$RERANK_PORT/v1/models (up to ${VERIFY_TIMEOUT_S}s;"
echo "[install-reranker] first start downloads ~1.2 GB of model weights)..."
ELAPSED=0
while (( ELAPSED < VERIFY_TIMEOUT_S )); do
    if curl --silent --fail --max-time 5 \
            "http://127.0.0.1:$RERANK_PORT/v1/models" > /dev/null 2>&1; then
        echo "[install-reranker] Reranker is up and serving on port $RERANK_PORT."
        echo "[install-reranker] To activate reranked search, follow the checklist in"
        echo "[install-reranker] Orchestrator/rerank.py (or the setup wizard's Memory & Search"
        echo "[install-reranker] step): set [rerank] provider = vllm and [retrieval]"
        echo "[install-reranker] rerank_enabled = true in config.ini, then restart blackbox.service."
        exit 0
    fi
    # Fail fast on TERMINAL unit failure instead of burning the whole timeout
    # window. is-failed only reports the failed end-state — transient
    # Restart=on-failure cycles ("activating (auto-restart)") keep waiting.
    if sudo systemctl is-failed --quiet vllm-reranker.service; then
        echo "[install-reranker] ERROR: vllm-reranker.service entered the failed state" >&2
        echo "[install-reranker] Check 'journalctl -u vllm-reranker.service -n 100' (common causes:" >&2
        echo "[install-reranker] CUDA driver mismatch, VRAM already exhausted, no network to Hugging Face)," >&2
        echo "[install-reranker] then re-run: sudo bash installer/templates/blackbox-install-reranker.sh" >&2
        exit 6
    fi
    sleep 5
    ELAPSED=$(( ELAPSED + 5 ))
    if (( ELAPSED % 30 == 0 )); then
        echo "[install-reranker] ...still waiting (${ELAPSED}s; model load/download in progress)"
    fi
done

echo "[install-reranker] ERROR: reranker did not answer on port $RERANK_PORT within ${VERIFY_TIMEOUT_S}s" >&2
echo "[install-reranker] The service stays enabled and may still finish its first-boot model" >&2
echo "[install-reranker] download. Check 'journalctl -u vllm-reranker.service -f'; once" >&2
echo "[install-reranker] 'curl http://127.0.0.1:$RERANK_PORT/v1/models' answers, no re-run is needed." >&2
exit 6
