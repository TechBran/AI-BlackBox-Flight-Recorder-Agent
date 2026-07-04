#!/usr/bin/env bash
# blackbox-install-reranker-cpu — MID-tier CPU reranker deps (retrieval M5).
# The MID-tier sibling of blackbox-install-reranker.sh (the GPU/vLLM script):
# instead of a separate service it installs the in-process CrossEncoder stack
# (CPU-only torch + sentence-transformers) INTO THE ORCHESTRATOR VENV and warms
# the Hugging Face cache for Qwen/Qwen3-Reranker-0.6B. Orchestrator/rerank.py's
# `cpu` provider (_score_cpu) lazy-imports these at retrieve()-time — no second
# service, no GPU. Enablement stays a deliberate operator selection (the wizard
# Memory & Search step / [rerank] provider = cpu); nothing here writes config.
#
# TIER GATE (via Orchestrator/hardware.py's probe() — the single source of truth
# for LOW/MID/HIGH, reused rather than re-implemented in bash):
#   GPU present            -> skip (that box uses the GPU vLLM reranker) + exit 0
#   no GPU, RAM >= 32 GB    -> MID: install the CPU reranker deps
#   no GPU, RAM  < 32 GB    -> LOW: skip (cloud-only reranking) + exit 0
# The CPU stack (torch + sentence-transformers) is a ~2 GB download; the gate
# keeps it off LOW boxes and off GPU boxes. Mirrors the GPU script's
# skip-clean-exit-0 pattern so install.sh can call it unconditionally.
#
# SHARED VENV (deliberate, per the M5 design): unlike the GPU script's dedicated
# $REAL_HOME/rerank-venv, the CPU deps go into $BLACKBOX_ROOT/Orchestrator/venv
# because _score_cpu runs IN-PROCESS in the Orchestrator (the FastAPI threadpool
# where retrieve() already executes). torch/sentence-transformers are therefore
# NOT in requirements.txt — installer-added + lazy-imported, so a fresh LOW box
# stays import-clean.
#
# IDEMPOTENT / re-run safe: pip re-install of a satisfied requirement is a
# no-op; a cached model re-download is a no-op.
#
# Usage (install.sh passes all three; standalone auto-detects):
#   sudo bash installer/templates/blackbox-install-reranker-cpu.sh \
#       [real_user] [real_home] [blackbox_root]
#
# Exit codes:
#   0 — installed (or a clean LOW/GPU skip)
#   2 — could not resolve user/home/root, or the Orchestrator venv is missing
#   4 — pip install or model pre-download failed
#
# install.sh invokes this NON-FATALLY (like the Ollama + GPU-reranker steps):
# retrieval works un-reranked, and the wizard's Memory & Search step shows the
# remediation.

set -euo pipefail

REAL_USER="${1:-}"
REAL_HOME="${2:-}"
BLACKBOX_ROOT="${3:-}"

# ── Resolve user/home/root (mirrors the GPU sibling for standalone runs) ──────
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
    echo "[install-reranker-cpu] ERROR: could not resolve user/home (got user='$REAL_USER' home='$REAL_HOME')" >&2
    exit 2
fi
if [[ -z "$BLACKBOX_ROOT" ]]; then
    # This script lives at $BLACKBOX_ROOT/installer/templates/ — two dirs up.
    BLACKBOX_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi

MODEL_ID="Qwen/Qwen3-Reranker-0.6B"
ORCH_VENV="$BLACKBOX_ROOT/Orchestrator/venv"
ORCH_PY="$ORCH_VENV/bin/python"
ORCH_PIP="$ORCH_VENV/bin/pip"
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"

# The Orchestrator venv is created earlier in install.sh (Step 2). A missing one
# means the main installer has not run — a precondition failure, not a skip.
if [[ ! -x "$ORCH_PIP" ]]; then
    echo "[install-reranker-cpu] ERROR: Orchestrator venv not found at $ORCH_VENV" >&2
    echo "[install-reranker-cpu] (Run the main installer first, then re-run this step.)" >&2
    exit 2
fi

# ── Tier gate: reuse Orchestrator/hardware.py's probe() (single source of truth
# for LOW/MID/HIGH). hardware.py is stdlib-only, so this needs no third-party
# deps. Run as $REAL_USER (venv ownership) with bytecode-writes off (no root pyc
# pollution). Prints "TIER GPUFLAG RAM_MB".
PROBE="$(sudo -u "$REAL_USER" env PYTHONDONTWRITEBYTECODE=1 "$ORCH_PY" -c \
    "import sys; sys.path.insert(0, '$BLACKBOX_ROOT'); from Orchestrator.hardware import probe; p = probe(); print(p['tier'], int(bool(p['gpu'])), int(p['ram_mb']))" \
    2>/dev/null || true)"
read -r TIER GPU_FLAG RAM_MB <<< "$PROBE"

if [[ -z "$TIER" ]]; then
    echo "[install-reranker-cpu] Could not determine hardware tier (probe failed) — skipping."
    echo "[install-reranker-cpu] search works fully without the CPU reranker."
    exit 0
fi
if [[ "$GPU_FLAG" == "1" ]]; then
    echo "[install-reranker-cpu] NVIDIA GPU detected — this box uses the GPU vLLM reranker"
    echo "[install-reranker-cpu] (blackbox-install-reranker.sh); skipping the CPU path."
    exit 0
fi
if [[ "$TIER" != "MID" ]]; then
    RAM_GB=$(( ${RAM_MB:-0} / 1024 ))
    echo "[install-reranker-cpu] The CPU reranker is a 32GB+ (MID-tier) feature; this box has"
    echo "[install-reranker-cpu] ${RAM_GB} GB RAM (tier ${TIER}) — skipping. Search works fully"
    echo "[install-reranker-cpu] without it; on this box reranking is cloud-only."
    exit 0
fi
RAM_GB=$(( ${RAM_MB:-0} / 1024 ))
echo "[install-reranker-cpu] MID tier (${RAM_GB} GB RAM, no GPU) — installing the in-process CPU reranker deps."

# ── Install CPU-only torch + sentence-transformers into the Orchestrator venv ──
# TWO commands on purpose: --index-url REPLACES PyPI as the primary index, and
# sentence-transformers is NOT hosted on the PyTorch CPU index — so torch (the
# +cpu build, avoiding the multi-GB CUDA wheels) is installed from the pytorch
# index first, then sentence-transformers is pulled from PyPI (its torch dep is
# already satisfied by the CPU build). Plain install (no --upgrade) is a cheap
# no-op on re-run when the requirement is already satisfied.
echo "[install-reranker-cpu] Installing CPU-only torch from $TORCH_CPU_INDEX (large download on first install)..."
if ! sudo -u "$REAL_USER" "$ORCH_PIP" install torch --index-url "$TORCH_CPU_INDEX"; then
    echo "[install-reranker-cpu] ERROR: pip install of CPU torch failed" >&2
    echo "[install-reranker-cpu] (Check network/disk, then re-run" >&2
    echo "[install-reranker-cpu]  sudo bash installer/templates/blackbox-install-reranker-cpu.sh)" >&2
    exit 4
fi
echo "[install-reranker-cpu] Installing sentence-transformers from PyPI..."
if ! sudo -u "$REAL_USER" "$ORCH_PIP" install sentence-transformers; then
    echo "[install-reranker-cpu] ERROR: pip install of sentence-transformers failed" >&2
    echo "[install-reranker-cpu] (Check network/disk, then re-run" >&2
    echo "[install-reranker-cpu]  sudo bash installer/templates/blackbox-install-reranker-cpu.sh)" >&2
    exit 4
fi

# ── Pre-download the model (warms the HF cache under $REAL_HOME/.cache) ─────────
# Doubles as the import+load verification: if CrossEncoder(MODEL_ID) succeeds,
# both the sentence-transformers import and the weight download worked. A cached
# model makes this a no-op on re-run.
echo "[install-reranker-cpu] Pre-downloading $MODEL_ID (warms the Hugging Face cache; ~1.2 GB on first run)..."
if ! sudo -u "$REAL_USER" "$ORCH_PY" -c \
        "from sentence_transformers import CrossEncoder; CrossEncoder('$MODEL_ID')"; then
    echo "[install-reranker-cpu] ERROR: model pre-download / CrossEncoder load failed for $MODEL_ID" >&2
    echo "[install-reranker-cpu] (Check network to Hugging Face + disk space, then re-run" >&2
    echo "[install-reranker-cpu]  sudo bash installer/templates/blackbox-install-reranker-cpu.sh)" >&2
    exit 4
fi

echo "[install-reranker-cpu] CPU reranker deps installed + $MODEL_ID cached."
echo "[install-reranker-cpu] To activate reranked search, select the CPU reranker in the setup"
echo "[install-reranker-cpu] wizard's Memory & Search step (or set [rerank] provider = cpu +"
echo "[install-reranker-cpu] model = qwen3-reranker-0.6b-cpu and [retrieval] rerank_enabled = true"
echo "[install-reranker-cpu] in config.ini), then restart blackbox.service."
exit 0
