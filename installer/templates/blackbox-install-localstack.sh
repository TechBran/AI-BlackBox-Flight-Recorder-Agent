#!/usr/bin/env bash
# blackbox-install-localstack — provision the on-box local-model stack
# (local-model-stack plan, Milestone 2). Stands up llama-swap (the :9098 front
# door), the llama.cpp llama-server binary, the Speaches + qwen-tts venvs, the
# generated llama-swap config.yaml, and blackbox-models.service. Modeled on
# blackbox-install-reranker.sh.
#
# Unlike the reranker, this runs on BOTH GPU and CPU boxes: the nvidia-smi
# gate selects the llama.cpp BUILD (CUDA vs CPU), it is NOT an exit-0 skip.
# (vLLM stays GPU-only in blackbox-install-reranker.sh; this is the always-on
# stack.) NO weights are downloaded here — that happens in the wizard,
# disk-gated (POST /local-models/download).
#
# IDEMPOTENT / re-run safe:
#   - llama-swap binary at pinned version -> skip download
#   - llama-server marker == pinned tag   -> skip download
#   - venv exists                         -> pip --upgrade path
#   - unit exists                         -> re-written + daemon-reload
#   - service running                     -> restarted + re-verified
#
# Usage (install.sh Step 2f passes all three; standalone auto-detects):
#   sudo bash installer/templates/blackbox-install-localstack.sh \
#       [real_user] [real_home] [blackbox_root]
#
# Exit codes:
#   0 — provisioned + llama-swap answering on :9098/health
#   2 — could not resolve user/home
#   4 — download/verify or venv creation failed
#   6 — blackbox-models.service never answered on :9098 in time
#
# install.sh invokes this NON-FATALLY: cloud STT/TTS/embeddings/rerank keep
# working and the wizard's local_models step shows the remediation.

set -euo pipefail

REAL_USER="${1:-}"
REAL_HOME="${2:-}"
BLACKBOX_ROOT="${3:-}"

# ── Resolve user/home/root (mirrors blackbox-install-reranker.sh) ──────────
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
    echo "[install-localstack] ERROR: could not resolve user/home (got user='$REAL_USER' home='$REAL_HOME')" >&2
    exit 2
fi
if [[ -z "$BLACKBOX_ROOT" ]]; then
    BLACKBOX_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi

TEMPLATE_DIR="$BLACKBOX_ROOT/installer/templates"
LOCALSTACK_HOME="$REAL_HOME/.blackbox/localstack"
LOCALSTACK_BIN="$LOCALSTACK_HOME/bin"
LOCALSTACK_MODELS="$LOCALSTACK_HOME/models"
SPEACHES_VENV="$LOCALSTACK_HOME/speaches-venv"
QWEN_TTS_VENV="$LOCALSTACK_HOME/qwen-tts-venv"
CONFIG_DEST="$LOCALSTACK_HOME/llama-swap-config.yaml"
FRONT_PORT=9098
VERIFY_TIMEOUT_S=180
# Speaches pin (pre-1.0; §5.3). qwen-tts deps come from the repo requirements
# (TTS milestone); a fastapi/uvicorn floor keeps the member's uvicorn present.
SPEACHES_PIN="speaches==0.9.0rc3"

PYBIN="$(command -v python3.12 || command -v python3)"

# Everything lives under the user's home, owned by REAL_USER.
sudo -u "$REAL_USER" mkdir -p "$LOCALSTACK_BIN" "$LOCALSTACK_MODELS"

# ── GPU build selector (NOT a skip) ────────────────────────────────────────
USE_CUDA=0
if command -v nvidia-smi >/dev/null 2>&1; then
    VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
        | sort -nr | head -n1 | tr -d '[:space:]' || true)"
    if [[ "$VRAM_MB" =~ ^[0-9]+$ ]] && (( VRAM_MB >= 8000 )); then
        USE_CUDA=1
        echo "[install-localstack] GPU detected (${VRAM_MB} MB VRAM) — CUDA llama-server build."
    else
        echo "[install-localstack] GPU present but VRAM <8000 MB (or unreadable) — CPU llama-server build."
    fi
else
    echo "[install-localstack] No NVIDIA GPU — CPU llama-server build (honest CPU tier, §7)."
fi

# ── 1. llama-swap release binary (sha256-verified; zellij pattern) ─────────
LS_VER="$(grep -vE '^[[:space:]]*(#|$)' "$TEMPLATE_DIR/llama-swap-version" | head -n1 | tr -d '[:space:]')"
if [[ -z "$LS_VER" ]]; then
    echo "[install-localstack] ERROR: could not parse pinned llama-swap version" >&2
    exit 4
fi
need_ls=1
if [[ -x "$LOCALSTACK_BIN/llama-swap" ]]; then
    cur="$("$LOCALSTACK_BIN/llama-swap" --version 2>/dev/null | grep -oE '[0-9]+' | head -n1 || true)"
    [[ "$cur" == "$LS_VER" ]] && { need_ls=0; echo "[install-localstack] llama-swap $LS_VER already installed, skipping."; }
fi
if (( need_ls )); then
    TMP_LS="$(mktemp -d /tmp/llama-swap-XXXXXX)"
    trap 'rm -rf "${TMP_LS:-}"' EXIT
    LS_TARBALL="llama-swap_${LS_VER}_linux_amd64.tar.gz"
    LS_BASE="https://github.com/mostlygeek/llama-swap/releases/download/v${LS_VER}"
    echo "[install-localstack] Downloading llama-swap v${LS_VER}..."
    if ! curl --fail --location --silent --show-error -o "$TMP_LS/$LS_TARBALL" "$LS_BASE/$LS_TARBALL" \
       || ! curl --fail --location --silent --show-error -o "$TMP_LS/checksums.txt" "$LS_BASE/checksums.txt"; then
        echo "[install-localstack] ERROR: llama-swap download failed ($LS_BASE)" >&2
        exit 4
    fi
    LS_EXPECTED="$(awk -v f="$LS_TARBALL" '$2==f || $2=="*"f {print $1}' "$TMP_LS/checksums.txt" | head -n1)"
    LS_ACTUAL="$(sha256sum "$TMP_LS/$LS_TARBALL" | awk '{print $1}')"
    if [[ -z "$LS_EXPECTED" || "$LS_EXPECTED" != "$LS_ACTUAL" ]]; then
        echo "[install-localstack] ERROR: llama-swap sha256 mismatch (expected='$LS_EXPECTED' actual='$LS_ACTUAL')" >&2
        exit 4
    fi
    tar -xzf "$TMP_LS/$LS_TARBALL" -C "$TMP_LS"
    LS_SRC="$(find "$TMP_LS" -type f -name llama-swap | head -n1)"
    if [[ -z "$LS_SRC" ]]; then
        echo "[install-localstack] ERROR: llama-swap binary not found after extract" >&2
        exit 4
    fi
    sudo install -m 0755 -o "$REAL_USER" -g "$REAL_USER" "$LS_SRC" "$LOCALSTACK_BIN/llama-swap"
    echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-swap (v$LS_VER, sha256 $LS_ACTUAL)"
    rm -rf "$TMP_LS"; trap - EXIT
fi

# ── 2. llama.cpp llama-server prebuilt (CUDA behind the gate; sha-pinned) ──
mapfile -t LC_PINS < <(grep -vE '^[[:space:]]*(#|$)' "$TEMPLATE_DIR/llamacpp-version")
LC_VER="${LC_PINS[0]:-}"; LC_CPU_SHA="${LC_PINS[1]:-}"; LC_CUDA_SHA="${LC_PINS[2]:-}"
if [[ -z "$LC_VER" ]]; then
    echo "[install-localstack] ERROR: could not parse pinned llama.cpp version" >&2
    exit 4
fi
# Confirm asset names on the release page if goreleaser/CI naming drifts.
ASSET_CPU="llama-${LC_VER}-bin-ubuntu-x64.zip"
ASSET_CUDA="llama-${LC_VER}-bin-ubuntu-cuda-x64.zip"
if (( USE_CUDA )); then LC_ASSET="$ASSET_CUDA"; LC_SHA="$LC_CUDA_SHA"; else LC_ASSET="$ASSET_CPU"; LC_SHA="$LC_CPU_SHA"; fi
if [[ "$LC_SHA" == FILL_* || -z "$LC_SHA" ]]; then
    echo "[install-localstack] ERROR: llama.cpp sha256 not pinned for $LC_ASSET." >&2
    echo "[install-localstack] Fill it in $TEMPLATE_DIR/llamacpp-version (see the header remediation), then re-run." >&2
    exit 4
fi
need_lc=1
LC_MARKER="$LOCALSTACK_BIN/.llamacpp-version"
if [[ -x "$LOCALSTACK_BIN/llama-server" && -f "$LC_MARKER" ]]; then
    [[ "$(cat "$LC_MARKER" 2>/dev/null)" == "$LC_VER" ]] && { need_lc=0; echo "[install-localstack] llama-server $LC_VER already installed, skipping."; }
fi
if (( need_lc )); then
    TMP_LC="$(mktemp -d /tmp/llamacpp-XXXXXX)"
    trap 'rm -rf "${TMP_LC:-}"' EXIT
    LC_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LC_VER}/${LC_ASSET}"
    echo "[install-localstack] Downloading llama.cpp $LC_VER ($LC_ASSET)..."
    if ! curl --fail --location --silent --show-error -o "$TMP_LC/$LC_ASSET" "$LC_URL"; then
        echo "[install-localstack] ERROR: llama.cpp download failed ($LC_URL)" >&2
        exit 4
    fi
    LC_ACTUAL="$(sha256sum "$TMP_LC/$LC_ASSET" | awk '{print $1}')"
    if [[ "$LC_SHA" != "$LC_ACTUAL" ]]; then
        echo "[install-localstack] ERROR: llama.cpp sha256 mismatch (expected='$LC_SHA' actual='$LC_ACTUAL')" >&2
        exit 4
    fi
    # python3 zipfile — no unzip dependency (python3 is MUST_HAVE).
    "$PYBIN" -m zipfile -e "$TMP_LC/$LC_ASSET" "$TMP_LC/x"
    LC_SRC="$(find "$TMP_LC/x" -type f -name llama-server | head -n1)"
    if [[ -z "$LC_SRC" ]]; then
        echo "[install-localstack] ERROR: llama-server not found in $LC_ASSET after extract" >&2
        exit 4
    fi
    # Copy the WHOLE bin dir (CUDA prebuilt bundles libllama/libggml *.so beside
    # the binary; LD_LIBRARY_PATH in the unit points at $LOCALSTACK_BIN).
    LC_SRCDIR="$(dirname "$LC_SRC")"
    sudo -u "$REAL_USER" cp -a "$LC_SRCDIR/." "$LOCALSTACK_BIN/"
    sudo -u "$REAL_USER" chmod +x "$LOCALSTACK_BIN/llama-server"
    echo "$LC_VER" | sudo -u "$REAL_USER" tee "$LC_MARKER" >/dev/null
    echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-server ($LC_VER, sha256 $LC_ACTUAL)"
    rm -rf "$TMP_LC"; trap - EXIT
fi

# ── 3. Speaches venv (own lean venv — the MCP lean-venv lesson) ────────────
if [[ ! -x "$SPEACHES_VENV/bin/pip" ]]; then
    echo "[install-localstack] Creating Speaches venv at $SPEACHES_VENV..."
    if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$SPEACHES_VENV"; then
        echo "[install-localstack] ERROR: Speaches venv creation failed" >&2
        exit 4
    fi
fi
echo "[install-localstack] Installing/upgrading Speaches ($SPEACHES_PIN)..."
if ! sudo -u "$REAL_USER" "$SPEACHES_VENV/bin/pip" install --upgrade "$SPEACHES_PIN"; then
    echo "[install-localstack] ERROR: pip install $SPEACHES_PIN failed (check the pinned version/network)" >&2
    exit 4
fi

# ── 4. qwen-tts venv (server code + deps come from the TTS milestone) ──────
if [[ ! -x "$QWEN_TTS_VENV/bin/pip" ]]; then
    echo "[install-localstack] Creating qwen-tts venv at $QWEN_TTS_VENV..."
    if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$QWEN_TTS_VENV"; then
        echo "[install-localstack] ERROR: qwen-tts venv creation failed" >&2
        exit 4
    fi
fi
QWEN_REQ="$BLACKBOX_ROOT/LocalModels/qwen_tts_server/requirements.txt"
if [[ -f "$QWEN_REQ" ]]; then
    echo "[install-localstack] Installing qwen-tts requirements..."
    if ! sudo -u "$REAL_USER" "$QWEN_TTS_VENV/bin/pip" install --upgrade -r "$QWEN_REQ"; then
        echo "[install-localstack] ERROR: qwen-tts requirements install failed" >&2
        exit 4
    fi
else
    # Floor so the member's uvicorn entrypoint exists; the TTS milestone
    # lands qwen_tts_server + its full requirements later.
    echo "[install-localstack] (qwen-tts requirements.txt absent — installing fastapi/uvicorn floor; TTS milestone lands the server.)"
    sudo -u "$REAL_USER" "$QWEN_TTS_VENV/bin/pip" install --upgrade fastapi uvicorn || true
fi

# ── 5. Write llama-swap config.yaml from the template ──────────────────────
# Substitute ONLY the four localstack path vars; ${PORT}/${llama-server}/
# ${models-dir} stay literal for llama-swap. sed with | delimiter; $ before {
# is literal in BRE, backslash-escaped so bash does not expand it.
TMP_CFG="$(mktemp)"
sed -e "s|\${LOCALSTACK_BIN}|$LOCALSTACK_BIN|g" \
    -e "s|\${LOCALSTACK_MODELS}|$LOCALSTACK_MODELS|g" \
    -e "s|\${SPEACHES_VENV}|$SPEACHES_VENV|g" \
    -e "s|\${QWEN_TTS_VENV}|$QWEN_TTS_VENV|g" \
    "$TEMPLATE_DIR/llama-swap-config.yaml.template" > "$TMP_CFG"
sudo install -m 0644 -o "$REAL_USER" -g "$REAL_USER" "$TMP_CFG" "$CONFIG_DEST"
rm -f "$TMP_CFG"
echo "[install-localstack] Wrote $CONFIG_DEST"

# ── 6. Install blackbox-models.service (sed-template flow like reranker) ────
sed -e "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" \
    -e "s|REAL_HOME_PLACEHOLDER|$REAL_HOME|g" \
    -e "s|LOCALSTACK_HOME_PLACEHOLDER|$LOCALSTACK_HOME|g" \
    -e "s|LOCALSTACK_BIN_PLACEHOLDER|$LOCALSTACK_BIN|g" \
    "$TEMPLATE_DIR/blackbox-models.service" | sudo tee /etc/systemd/system/blackbox-models.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable blackbox-models.service >/dev/null 2>&1
sudo systemctl restart blackbox-models.service
echo "[install-localstack] blackbox-models.service written + enabled + (re)started"

# ── 7. Verify: llama-swap front door answers /health (members lazy-load, so
#      the front door is up with zero weights resident) ──────────────────────
echo "[install-localstack] Waiting for http://127.0.0.1:$FRONT_PORT/health (up to ${VERIFY_TIMEOUT_S}s)..."
ELAPSED=0
while (( ELAPSED < VERIFY_TIMEOUT_S )); do
    if curl --silent --fail --max-time 5 "http://127.0.0.1:$FRONT_PORT/health" >/dev/null 2>&1; then
        echo "[install-localstack] llama-swap front door is up on :$FRONT_PORT."
        echo "[install-localstack] Download weights + activate per capability in the wizard's"
        echo "[install-localstack] 'Local models' step; nothing activates implicitly on install."
        exit 0
    fi
    if sudo systemctl is-failed --quiet blackbox-models.service; then
        echo "[install-localstack] ERROR: blackbox-models.service entered the failed state" >&2
        echo "[install-localstack] Check 'journalctl -u blackbox-models.service -n 100'." >&2
        exit 6
    fi
    sleep 5; ELAPSED=$(( ELAPSED + 5 ))
done
echo "[install-localstack] ERROR: llama-swap did not answer on :$FRONT_PORT within ${VERIFY_TIMEOUT_S}s" >&2
echo "[install-localstack] The service stays enabled; check 'journalctl -u blackbox-models.service -f'." >&2
exit 6
