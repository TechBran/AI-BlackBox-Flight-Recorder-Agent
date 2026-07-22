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
# Speaches pin (pre-1.0; §5.3). It is NOT published to PyPI — install from the
# git tag (pip supports VCS URLs). qwen-tts deps come from the repo requirements
# (TTS milestone); a fastapi/uvicorn floor keeps the member's uvicorn present.
SPEACHES_PIN="git+https://github.com/speaches-ai/speaches.git@v0.9.0-rc.3"

PYBIN="$(command -v python3.12 || command -v python3)"

# Everything lives under the user's home, owned by REAL_USER.
sudo -u "$REAL_USER" mkdir -p "$LOCALSTACK_BIN" "$LOCALSTACK_MODELS"

# ── GPU build selector (NOT a skip) ────────────────────────────────────────
# USE_CUDA=1 means "attempt the CUDA SOURCE build (primary) with a Vulkan
# prebuilt fallback"; USE_CUDA=0 means "CPU prebuilt floor". See §2 for the
# three-tier acquisition — llama.cpp no longer ships prebuilt Linux CUDA
# binaries, so CUDA is compiled from source at the pinned tag.
USE_CUDA=0
if command -v nvidia-smi >/dev/null 2>&1; then
    VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
        | sort -nr | head -n1 | tr -d '[:space:]' || true)"
    if [[ "$VRAM_MB" =~ ^[0-9]+$ ]] && (( VRAM_MB >= 8000 )); then
        USE_CUDA=1
        echo "[install-localstack] GPU detected (${VRAM_MB} MB VRAM) — CUDA source build (Vulkan prebuilt fallback)."
    else
        echo "[install-localstack] GPU present but VRAM <8000 MB (or unreadable) — CPU llama-server prebuilt."
    fi
else
    echo "[install-localstack] No NVIDIA GPU — CPU llama-server prebuilt (honest CPU tier, §7)."
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
    # goreleaser publishes the checksum file VERSIONED (llama-swap_<VER>_checksums.txt),
    # not a bare checksums.txt — confirm the name on the release page if it drifts.
    LS_CHECKSUMS="llama-swap_${LS_VER}_checksums.txt"
    echo "[install-localstack] Downloading llama-swap v${LS_VER}..."
    if ! curl --fail --location --silent --show-error -o "$TMP_LS/$LS_TARBALL" "$LS_BASE/$LS_TARBALL" \
       || ! curl --fail --location --silent --show-error -o "$TMP_LS/$LS_CHECKSUMS" "$LS_BASE/$LS_CHECKSUMS"; then
        echo "[install-localstack] ERROR: llama-swap download failed ($LS_BASE)" >&2
        exit 4
    fi
    LS_EXPECTED="$(awk -v f="$LS_TARBALL" '$2==f || $2=="*"f {print $1}' "$TMP_LS/$LS_CHECKSUMS" | head -n1)"
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

# ── 2. llama.cpp llama-server (CUDA source build primary; Vulkan/CPU tar.gz
#      prebuilt fallbacks) ──────────────────────────────────────────────────
# llama.cpp stopped shipping prebuilt Linux CUDA binaries, so on a capable
# NVIDIA box we BUILD llama-server from source with CUDA at the pinned tag
# (sm_89 = RTX 2000 Ada). If that build cannot be produced we fall back to the
# Vulkan prebuilt; boxes with no capable GPU get the CPU prebuilt floor. The
# two shas pin the .tar.gz prebuilt fallbacks (there is no CUDA prebuilt sha —
# CUDA is compiled, not downloaded).
mapfile -t LC_PINS < <(grep -vE '^[[:space:]]*(#|$)' "$TEMPLATE_DIR/llamacpp-version")
LC_VER="${LC_PINS[0]:-}"; LC_CPU_SHA="${LC_PINS[1]:-}"; LC_VULKAN_SHA="${LC_PINS[2]:-}"
if [[ -z "$LC_VER" ]]; then
    echo "[install-localstack] ERROR: could not parse pinned llama.cpp version" >&2
    exit 4
fi
# Confirm asset names on the release page if goreleaser/CI naming drifts.
ASSET_CPU="llama-${LC_VER}-bin-ubuntu-x64.tar.gz"
ASSET_VULKAN="llama-${LC_VER}-bin-ubuntu-vulkan-x64.tar.gz"

# The backend we WANT: cuda on a capable NVIDIA box (Vulkan is only ever a
# fallback), else the cpu prebuilt floor. The marker records what we actually
# installed (e.g. b10084-cuda / b10084-vulkan / b10084-cpu).
if (( USE_CUDA )); then LC_WANT_BACKEND="cuda"; else LC_WANT_BACKEND="cpu"; fi
LC_MARKER="$LOCALSTACK_BIN/.llamacpp-version"
need_lc=1
if [[ -x "$LOCALSTACK_BIN/llama-server" && -f "$LC_MARKER" ]]; then
    if [[ "$(cat "$LC_MARKER" 2>/dev/null)" == "${LC_VER}-${LC_WANT_BACKEND}" ]]; then
        need_lc=0
        echo "[install-localstack] llama-server ${LC_VER} (${LC_WANT_BACKEND}) already installed, skipping."
    fi
fi
if (( need_lc )); then
    TMP_LC="$(mktemp -d /tmp/llamacpp-XXXXXX)"
    trap 'rm -rf "${TMP_LC:-}"' EXIT
    LC_INSTALLED_BACKEND=""

    # Install a prebuilt tar.gz fallback ($1 asset, $2 expected sha256): download,
    # verify the sha, extract with tar (.tar.gz — NOT zip), then install the WHOLE
    # extracted bin dir (the prebuilt bundles the *.so beside llama-server; the
    # unit's LD_LIBRARY_PATH points at $LOCALSTACK_BIN so they must sit there).
    # Copy AS ROOT (mirrors §1's `sudo install`): the root-owned mktemp dir is
    # mode 0700 so a `sudo -u "$REAL_USER" cp` cannot traverse it -> EACCES; chown
    # back to REAL_USER before the user-scoped chmod. Returns non-zero on failure
    # (caller falls through), never `exit`s.
    lc_fetch_prebuilt() {
        local asset="$1" expected="$2"
        local url="https://github.com/ggml-org/llama.cpp/releases/download/${LC_VER}/${asset}"
        if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
            echo "[install-localstack] ERROR: llama.cpp sha256 not pinned for $asset (see llamacpp-version header)." >&2
            return 1
        fi
        echo "[install-localstack] Downloading llama.cpp $LC_VER ($asset)..."
        if ! curl --fail --location --silent --show-error -o "$TMP_LC/$asset" "$url"; then
            echo "[install-localstack] ERROR: llama.cpp download failed ($url)" >&2
            return 1
        fi
        local actual
        actual="$(sha256sum "$TMP_LC/$asset" | awk '{print $1}')"
        if [[ "$expected" != "$actual" ]]; then
            echo "[install-localstack] ERROR: llama.cpp sha256 mismatch for $asset (expected='$expected' actual='$actual')" >&2
            return 1
        fi
        # .tar.gz prebuilt — extract with tar (python3 -m tarfile is an alt).
        mkdir -p "$TMP_LC/x"
        if ! tar -xzf "$TMP_LC/$asset" -C "$TMP_LC/x"; then
            echo "[install-localstack] ERROR: failed to extract $asset" >&2
            return 1
        fi
        local srv srcdir
        srv="$(find "$TMP_LC/x" -type f -name llama-server | head -n1)"
        if [[ -z "$srv" ]]; then
            echo "[install-localstack] ERROR: llama-server not found in $asset after extract" >&2
            return 1
        fi
        srcdir="$(dirname "$srv")"
        sudo cp -a "$srcdir/." "$LOCALSTACK_BIN/"
        sudo chown -R "$REAL_USER:$REAL_USER" "$LOCALSTACK_BIN"
        sudo -u "$REAL_USER" chmod +x "$LOCALSTACK_BIN/llama-server"
        echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-server ($LC_VER, prebuilt, sha256 $actual)"
        return 0
    }

    # ── CUDA source build (primary path on a capable NVIDIA box) ────────────────
    if (( USE_CUDA )); then
        echo "[install-localstack] Building llama.cpp $LC_VER from source with CUDA (primary path)..."
        CUDA_OK=1
        # Ensure a CUDA compiler. The installer runs as root; nvidia-cuda-toolkit
        # is a multi-GB install. On failure, fall through to the Vulkan prebuilt.
        if ! command -v nvcc >/dev/null 2>&1; then
            echo "[install-localstack] nvcc not found — installing nvidia-cuda-toolkit (multi-GB; this can take a while)..."
            if ! sudo apt-get install -y nvidia-cuda-toolkit; then
                echo "[install-localstack] WARNING: nvidia-cuda-toolkit install failed — falling back to the Vulkan prebuilt." >&2
                CUDA_OK=0
            fi
        fi
        if (( CUDA_OK )) && ! command -v nvcc >/dev/null 2>&1; then
            echo "[install-localstack] WARNING: nvcc still unavailable after install — falling back to the Vulkan prebuilt." >&2
            CUDA_OK=0
        fi
        # Shallow, pinned-tag clone.
        if (( CUDA_OK )) && ! git clone --depth 1 --branch "$LC_VER" \
                https://github.com/ggml-org/llama.cpp "$TMP_LC/llama.cpp"; then
            echo "[install-localstack] WARNING: llama.cpp clone at $LC_VER failed — falling back to the Vulkan prebuilt." >&2
            CUDA_OK=0
        fi
        # Ubuntu's nvidia-cuda-toolkit is CUDA 12.x whose nvcc rejects the system
        # default gcc-13 ("unsupported GNU version"); the toolkit pulls gcc-12 as a
        # dep, so pin it as the CUDA host compiler when present (fall back to a
        # lower gcc, else nvcc's default). Prevents a spurious Vulkan fallback on
        # Ubuntu 24.04.
        CUDA_HOST_CC="$(command -v gcc-12 || command -v gcc-11 || command -v gcc-10 || true)"
        CUDA_HOST_FLAG=(); [[ -n "$CUDA_HOST_CC" ]] && CUDA_HOST_FLAG=(-DCMAKE_CUDA_HOST_COMPILER="$CUDA_HOST_CC")
        # Configure + build only the llama-server target. sm_89 = RTX 2000 Ada;
        # LLAMA_CURL=OFF drops the libcurl build dependency.
        if (( CUDA_OK )) && { ! cmake -S "$TMP_LC/llama.cpp" -B "$TMP_LC/llama.cpp/build" \
                    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 \
                    -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release "${CUDA_HOST_FLAG[@]}" \
                || ! cmake --build "$TMP_LC/llama.cpp/build" --target llama-server -j"$(nproc)"; }; then
            echo "[install-localstack] WARNING: CUDA build failed — falling back to the Vulkan prebuilt." >&2
            CUDA_OK=0
        fi
        LC_SRV=""
        if (( CUDA_OK )); then
            LC_SRV="$(find "$TMP_LC/llama.cpp/build" -type f -name llama-server | head -n1)"
            if [[ -z "$LC_SRV" ]]; then
                echo "[install-localstack] WARNING: llama-server missing after CUDA build — falling back to the Vulkan prebuilt." >&2
                CUDA_OK=0
            fi
        fi
        if (( CUDA_OK )); then
            # Install the built binary PLUS every built shared lib beside it (the
            # unit's LD_LIBRARY_PATH points at $LOCALSTACK_BIN so the *.so must sit
            # there). Copy AS ROOT (mirrors §1), chown back to REAL_USER.
            # Include BOTH regular files AND symlinks (-type l): the build produces
            # SONAME symlinks (libllama-common.so.0 -> ...so.0.0.1) that the linker
            # needs at runtime; a bare `-type f` drops them and llama-server then
            # fails with "cannot open shared object file libllama-common.so.0".
            sudo cp -a "$LC_SRV" "$LOCALSTACK_BIN/"
            while IFS= read -r sofile; do
                sudo cp -a "$sofile" "$LOCALSTACK_BIN/"
            done < <(find "$TMP_LC/llama.cpp/build" \( -type f -o -type l \) -name '*.so*')
            sudo chown -R "$REAL_USER:$REAL_USER" "$LOCALSTACK_BIN"
            sudo -u "$REAL_USER" chmod +x "$LOCALSTACK_BIN/llama-server"
            LC_INSTALLED_BACKEND="cuda"
            echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-server ($LC_VER, CUDA source build, sm_89)"
        fi
    fi

    # ── Vulkan prebuilt fallback (GPU box where the CUDA build did not land) ────
    if (( USE_CUDA )) && [[ -z "$LC_INSTALLED_BACKEND" ]]; then
        if lc_fetch_prebuilt "$ASSET_VULKAN" "$LC_VULKAN_SHA"; then
            LC_INSTALLED_BACKEND="vulkan"
        fi
    fi

    # ── CPU prebuilt floor (no NVIDIA GPU, or VRAM <8000 MB) ────────────────────
    if (( ! USE_CUDA )) && [[ -z "$LC_INSTALLED_BACKEND" ]]; then
        if lc_fetch_prebuilt "$ASSET_CPU" "$LC_CPU_SHA"; then
            LC_INSTALLED_BACKEND="cpu"
        fi
    fi

    if [[ -z "$LC_INSTALLED_BACKEND" || ! -x "$LOCALSTACK_BIN/llama-server" ]]; then
        echo "[install-localstack] ERROR: llama-server could not be provisioned (CUDA build + prebuilt fallbacks all failed)." >&2
        exit 4
    fi
    echo "${LC_VER}-${LC_INSTALLED_BACKEND}" | sudo -u "$REAL_USER" tee "$LC_MARKER" >/dev/null
    rm -rf "$TMP_LC"; trap - EXIT
fi

# ── 3. Speaches venv (own lean venv — the MCP lean-venv lesson) ────────────
# NON-FATAL: the Speaches STT member is the AUDIO group; a failure here leaves
# the retrieval group (embeddings+reranker) fully functional — the audio member
# just won't answer until fixed. Do not block the whole stack on heavy GPU deps.
SPEACHES_OK=1
if [[ ! -x "$SPEACHES_VENV/bin/pip" ]]; then
    echo "[install-localstack] Creating Speaches venv at $SPEACHES_VENV..."
    if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$SPEACHES_VENV"; then
        echo "[install-localstack] WARNING: Speaches venv creation failed — STT audio member unavailable (retrieval group unaffected)." >&2
        SPEACHES_OK=0
    fi
fi
if (( SPEACHES_OK )); then
    echo "[install-localstack] Installing/upgrading Speaches ($SPEACHES_PIN)..."
    if ! sudo -u "$REAL_USER" "$SPEACHES_VENV/bin/pip" install --upgrade "$SPEACHES_PIN"; then
        echo "[install-localstack] WARNING: Speaches install failed — STT audio member unavailable (retrieval group unaffected). Re-run to retry." >&2
        SPEACHES_OK=0
    fi
fi
if (( SPEACHES_OK )); then
    # Speaches v0.9.0-rc.3 pins onnx-asr loosely (>=0.7.0); onnx-asr >=0.12 moved
    # NemoConformerTdt out of onnx_asr.models, so parakeet.py (imported at startup)
    # ImportErrors and the member exits prematurely. We only use whisper — pin the
    # last API-compatible onnx-asr so speaches.main imports.
    sudo -u "$REAL_USER" "$SPEACHES_VENV/bin/pip" install "onnx-asr==0.7.0" || \
        echo "[install-localstack] WARNING: onnx-asr pin failed — speaches STT may not import." >&2
    # create_app() unconditionally mounts StaticFiles(directory="realtime-console/dist")
    # (a frontend build absent from the pip package). Stub the dir so the mount
    # succeeds; the member runs with cwd=$LOCALSTACK_HOME (see the llama-swap tmpl)
    # and ENABLE_UI=false (its Gradio demo breaks on the installed gradio). The
    # /v1/realtime WEBSOCKET route is separate from this static mount — STT streaming
    # is unaffected.
    sudo -u "$REAL_USER" mkdir -p "$LOCALSTACK_HOME/realtime-console/dist"
    sudo -u "$REAL_USER" sh -c "printf '<!doctype html><title>speaches</title>' > '$LOCALSTACK_HOME/realtime-console/dist/index.html'"
fi

# ── 4. qwen-tts venv (server code + deps come from the TTS milestone) ──────
# NON-FATAL too (audio group). Server start/health only needs fastapi/uvicorn;
# the real Qwen3-TTS model backend + G3 come later.
QWEN_OK=1
if [[ ! -x "$QWEN_TTS_VENV/bin/pip" ]]; then
    echo "[install-localstack] Creating qwen-tts venv at $QWEN_TTS_VENV..."
    if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$QWEN_TTS_VENV"; then
        echo "[install-localstack] WARNING: qwen-tts venv creation failed — TTS audio member unavailable (retrieval group unaffected)." >&2
        QWEN_OK=0
    fi
fi
QWEN_REQ="$BLACKBOX_ROOT/LocalModels/qwen_tts_server/requirements.txt"
if (( QWEN_OK )) && [[ -f "$QWEN_REQ" ]]; then
    echo "[install-localstack] Installing qwen-tts requirements..."
    if ! sudo -u "$REAL_USER" "$QWEN_TTS_VENV/bin/pip" install --upgrade -r "$QWEN_REQ"; then
        echo "[install-localstack] WARNING: qwen-tts requirements install failed — TTS audio member unavailable (retrieval group unaffected). Re-run to retry." >&2
        QWEN_OK=0
    fi
elif (( QWEN_OK )); then
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
    -e "s|\${LOCALMODELS}|$BLACKBOX_ROOT/LocalModels|g" \
    -e "s|\${LOCALSTACK_HOME}|$LOCALSTACK_HOME|g" \
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
