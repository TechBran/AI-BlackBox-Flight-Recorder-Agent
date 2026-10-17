#!/usr/bin/env bash
# Tests for blackbox-install-localstack.sh's llama.cpp CUDA build (install.sh
# Step 2f).
#
# 2026-10-09 audit X04-security-clients-infra-15 / X06-ops-deploy-16 (rank 41):
# every NVIDIA box compiled llama-server with CMAKE_CUDA_ARCHITECTURES=89 (the
# RTX 2000 Ada). On any other generation the build succeeds and the binary dies
# at first model load with "no kernel image is available", and the
# `<tag>-cuda` build marker then suppressed the rebuild on every re-run. The
# audit also found $BLACKBOX_ROOT/$REAL_HOME templated into the llama-swap
# config's `sh -c '…'` strings and the unit without any charset check
# (X04-security-clients-infra-14).
#
# Proves, running the real script in a mktemp sandbox (PATH stubs for sudo,
# nvidia-smi, nvcc, git, cmake, systemctl, curl and the venv tools; nothing
# downloaded, nothing under /etc touched):
#   1. the CUDA build targets the GPUs actually present (native) and the marker
#      records their compute capability;
#   2. a re-run on the same GPU does not rebuild;
#   3. a GPU swap rebuilds and re-records the arch;
#   4. a legacy `<tag>-cuda` marker (an sm_89 build) is accepted on an sm_89
#      card without rebuilding, and rebuilt anywhere else;
#   5. a multi-GPU box records every arch;
#   6. a root or home outside the installer charset is refused before any change.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCALSTACK_SH="$REPO_ROOT/installer/templates/blackbox-install-localstack.sh"
# shellcheck source=scripts/tests/lib/installer_stubs.sh
source "$REPO_ROOT/scripts/tests/lib/installer_stubs.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$LOCALSTACK_SH" ]] || fail "missing $LOCALSTACK_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
stub_kit_init "$WORK"

USER_NAME="$(id -un)"
ROOT="$WORK/root"
HOME_DIR="$WORK/home"
mkdir -p "$ROOT/installer/templates" "$HOME_DIR"
for f in llama-swap-version llamacpp-version llama-swap-config.yaml.template blackbox-models.service; do
    cp "$REPO_ROOT/installer/templates/$f" "$ROOT/installer/templates/"
done
LS_VER="$(grep -vE '^[[:space:]]*(#|$)' "$ROOT/installer/templates/llama-swap-version" | head -n1 | tr -d '[:space:]')"
LC_VER="$(grep -vE '^[[:space:]]*(#|$)' "$ROOT/installer/templates/llamacpp-version" | head -n1 | tr -d '[:space:]')"
BIN="$HOME_DIR/.blackbox/localstack/bin"
MARKER="$BIN/.llamacpp-version"
# llama-swap already at the pin, so the run never reaches for the network.
mkdir -p "$BIN"
printf '#!/bin/sh\necho "version: %s (stub)"\n' "$LS_VER" > "$BIN/llama-swap"
chmod +x "$BIN/llama-swap"

run_stack() {  # <compute caps> — prints the exit code
    : > "$STUB_LOG"
    STUB_COMPUTE_CAP="$1" STUB_VRAM_MB=16380 \
        bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$ROOT" > "$WORK/run.log" 2>&1
    echo $?
}
built() { stub_log_has '^cmake -S '; }

# ── 1. first build on an Ampere card ──────────────────────────────────────────
rc="$(run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "1: localstack run exited $rc: $(tail -20 "$WORK/run.log")"
built || fail "1: llama.cpp was not configured with cmake"
stub_log_has '^cmake -S .*-DCMAKE_CUDA_ARCHITECTURES=native( |$)' \
    || fail "1: the CUDA build does not target the GPUs present: $(grep '^cmake -S' "$STUB_LOG")"
stub_log_has 'CMAKE_CUDA_ARCHITECTURES=89' && fail "1: the CUDA build still hardcodes sm_89"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm86" ]] \
    || fail "1: build marker is '$(cat "$MARKER")', expected ${LC_VER}-cuda-sm86"
grep -q "PYTHONPATH=$ROOT/LocalModels exec" "$HOME_DIR/.blackbox/localstack/llama-swap-config.yaml" \
    || fail "1: llama-swap config did not template the install root"

# ── 2. same GPU: no rebuild ───────────────────────────────────────────────────
rc="$(run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "2: re-run exited $rc: $(tail -20 "$WORK/run.log")"
built && fail "2: llama.cpp was rebuilt although the GPU did not change"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm86" ]] || fail "2: marker changed on a no-op re-run"

# ── 3. GPU swap: rebuild ──────────────────────────────────────────────────────
rc="$(run_stack 8.9)"
[[ "$rc" == 0 ]] || fail "3: run exited $rc"
built || fail "3: a GPU swap (sm_86 -> sm_89) did not trigger a rebuild"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm89" ]] || fail "3: marker is '$(cat "$MARKER")' after the swap"

# ── 4. legacy marker from the sm_89-hardcoded builds ──────────────────────────
echo "${LC_VER}-cuda" > "$MARKER"
rc="$(run_stack 8.9)"
[[ "$rc" == 0 ]] || fail "4: run exited $rc"
built && fail "4: a legacy ${LC_VER}-cuda (sm_89) build was rebuilt on an sm_89 card"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm89" ]] || fail "4: the legacy marker was not upgraded to record sm89"
echo "${LC_VER}-cuda" > "$MARKER"
rc="$(run_stack 7.5)"
[[ "$rc" == 0 ]] || fail "4: run exited $rc"
built || fail "4: a legacy sm_89 build was kept on an sm_75 card"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm75" ]] || fail "4: marker is '$(cat "$MARKER")' on the sm_75 card"

# ── 5. two GPUs of different generations ──────────────────────────────────────
rc="$(run_stack "$(printf '8.9\n8.6')")"
[[ "$rc" == 0 ]] || fail "5: run exited $rc"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm86+89" ]] || fail "5: multi-GPU marker is '$(cat "$MARKER")'"

# ── 6. unsafe paths are refused before anything runs ─────────────────────────
for bad in "$WORK/r&d lab/root" "$WORK/q'uote" "$WORK/50%off"; do
    : > "$STUB_LOG"
    bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$bad" > "$WORK/bad.log" 2>&1; rc=$?
    [[ "$rc" == 2 ]] || fail "6: root '$bad' was not refused with exit 2 (rc=$rc)"
    [[ -s "$STUB_LOG" ]] && fail "6: commands ran before refusing root '$bad': $(cat "$STUB_LOG")"
done
: > "$STUB_LOG"
bash "$LOCALSTACK_SH" "$USER_NAME" "$WORK/a home" "$ROOT" > "$WORK/bad.log" 2>&1; rc=$?
[[ "$rc" == 2 ]] || fail "6: a home with a space was not refused with exit 2 (rc=$rc)"
[[ -s "$STUB_LOG" ]] && fail "6: commands ran before refusing the home"

echo "ALL TESTS PASSED"
exit 0
