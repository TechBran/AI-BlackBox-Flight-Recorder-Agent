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
#   1. the CUDA build targets the GPUs actually present and the marker records
#      their compute capability;
#   2. a re-run on the same GPU does not rebuild;
#   3. a GPU swap rebuilds and re-records the arch;
#   4. a legacy `<tag>-cuda` marker (an sm_89 build) is accepted on an sm_89
#      card without rebuilding, and rebuilt anywhere else;
#   5. a multi-GPU box records every arch;
#   6. a root or home outside the installer charset is refused before any change;
#   7. the architecture list is clamped to what the installed nvcc can target:
#      a card newer than the toolkit (Blackwell sm_120 on Ubuntu's apt CUDA
#      12.0, which stops at compute_90) still gets a CUDA build through PTX the
#      driver JIT-compiles, as the old hardcoded `89` did — CMake's `native` is
#      machine code only and fails to configure there — and is not rebuilt on
#      every re-run; a card older than anything nvcc supports falls back to
#      Vulkan without cloning; unknown caps or an nvcc without --list-gpu-arch
#      leave the choice to CMake (`native`).
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
for f in llama-swap-version llamacpp-version llama-swap-config.yaml.template blackbox-models.service \
         blackbox-gpu-preflight.sh; do
    cp "$REPO_ROOT/installer/templates/$f" "$ROOT/installer/templates/"
done
# Step 5's config renderer (Orchestrator/localstack_render.py, stdlib only).
mkdir -p "$ROOT/Orchestrator"
cp "$REPO_ROOT/Orchestrator/localstack_render.py" "$ROOT/Orchestrator/"
# Step 4's pinned qwen-tts requirements + lock.
mkdir -p "$ROOT/LocalModels/qwen_tts_server"
cp "$REPO_ROOT/LocalModels/qwen_tts_server/requirements.txt" \
   "$REPO_ROOT/LocalModels/qwen_tts_server/requirements.lock.txt" "$ROOT/LocalModels/qwen_tts_server/"
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
cuda_archs() {  # the CMAKE_CUDA_ARCHITECTURES value of the last configure
    grep '^cmake -S ' "$STUB_LOG" | grep -oE -- '-DCMAKE_CUDA_ARCHITECTURES=[^ ]*' | head -n1 | cut -d= -f2
}

# ── 1. first build on an Ampere card ──────────────────────────────────────────
rc="$(run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "1: localstack run exited $rc: $(tail -20 "$WORK/run.log")"
built || fail "1: llama.cpp was not configured with cmake"
# `86` = sm_86 machine code plus compute_86 PTX (CMake's real+virtual).
[[ "$(cuda_archs)" == 86 ]] \
    || fail "1: the CUDA build does not target the sm_86 card present: $(grep '^cmake -S' "$STUB_LOG")"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm86" ]] \
    || fail "1: build marker is '$(cat "$MARKER")', expected ${LC_VER}-cuda-sm86"
grep -q "PYTHONPATH=$ROOT/LocalModels exec" "$HOME_DIR/.blackbox/localstack/llama-swap-config.yaml" \
    || fail "1: llama-swap config did not template the install root"
# qwen-tts venv: pinned requirements under the lock, then a CUDA check on a GPU box.
QWEN_DIR="$ROOT/LocalModels/qwen_tts_server"
stub_log_has "^pip\[$HOME_DIR/.blackbox/localstack/qwen-tts-venv\] install --upgrade -r $QWEN_DIR/requirements.txt -c $QWEN_DIR/requirements.lock.txt\$" \
    || fail "1: the qwen-tts venv was not installed under the lock: $(grep 'qwen-tts-venv' "$STUB_LOG")"
stub_log_has "^venv-python\[$HOME_DIR/.blackbox/localstack/qwen-tts-venv\] -c .*torch.cuda.is_available" \
    || fail "1: the qwen-tts venv's torch was not checked against the GPU"

# ── 2. same GPU: no rebuild ───────────────────────────────────────────────────
rc="$(run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "2: re-run exited $rc: $(tail -20 "$WORK/run.log")"
built && fail "2: llama.cpp was rebuilt although the GPU did not change"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm86" ]] || fail "2: marker changed on a no-op re-run"

# ── 2b. a re-run keeps the box's keep-warm choices ───────────────────────────
# Step 5 used to re-sed the template and reset every member to ttl 600
# (audit O19-local-stack-09); the renderer applies the recorded overrides.
CFG="$HOME_DIR/.blackbox/localstack/llama-swap-config.yaml"
printf '{"ttl": {"embed-qwen3-8b": 0}}\n' > "$HOME_DIR/.blackbox/localstack/llama-swap-overrides.json"
rc="$(run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "2b: re-run exited $rc: $(tail -20 "$WORK/run.log")"
grep -A 40 '^  "embed-qwen3-8b":' "$CFG" | grep -m 1 -E '^    ttl:' | grep -qE '^    ttl: 0 ' \
    || fail "2b: the installer re-run reset the keep-warm embed member: $(grep -E '^    ttl:' "$CFG")"
grep -q 'keep-warm' "$CFG" || fail "2b: the rendered config lost the template comments"

# ── 2c. a qwen-tts torch that cannot reach the GPU is said out loud ───────────
: > "$STUB_LOG"
STUB_VENV_PYTHON_RC=1 STUB_COMPUTE_CAP=8.6 STUB_VRAM_MB=16380 \
    bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$ROOT" > "$WORK/nocuda.log" 2>&1; rc=$?
[[ "$rc" == 0 ]] || fail "2c: a CUDA-less qwen-tts venv must not fail the whole stack (rc=$rc)"
grep -q 'torch.cuda.is_available() is False' "$WORK/nocuda.log" \
    || fail "2c: a qwen-tts torch that cannot use the GPU was not reported: $(grep -i qwen "$WORK/nocuda.log")"

# ── 2d. a re-run after three failed starts still restarts the stack ───────────
# blackbox-models.service carries StartLimitBurst=3 within 300 s, and manual
# starts count against it: re-running the installer right after a driver fix
# (what the exit-7 message asks for) hit "start request repeated too quickly"
# and `set -e` aborted the run with an undocumented exit 1.
: > "$STUB_LOG"
STUB_START_LIMITED=blackbox-models.service STUB_COMPUTE_CAP=8.6 STUB_VRAM_MB=16380 \
    bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$ROOT" > "$WORK/limited.log" 2>&1; rc=$?
[[ "$rc" == 0 ]] || fail "2d: a start-limited unit aborted the re-run (rc=$rc): $(tail -5 "$WORK/limited.log")"
grep -nE '^systemctl (reset-failed|restart) blackbox-models.service$' "$STUB_LOG" | head -n 1 \
    | grep -q 'reset-failed' || fail "2d: blackbox-models.service was restarted without a reset-failed first"

# ── 3. GPU swap: rebuild ──────────────────────────────────────────────────────
rc="$(run_stack 8.9)"
[[ "$rc" == 0 ]] || fail "3: run exited $rc"
built || fail "3: a GPU swap (sm_86 -> sm_89) did not trigger a rebuild"
[[ "$(cuda_archs)" == 89 ]] || fail "3: the sm_89 rebuild targets '$(cuda_archs)'"
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
[[ "$(cuda_archs)" == "86-real;89" ]] || fail "5: the two-GPU build targets '$(cuda_archs)', want 86-real;89"

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

# ── 7. architectures clamped to the installed nvcc ────────────────────────────
# Blackwell (sm_120) on CUDA 12.0: no machine code is possible, compute_90 PTX
# is, and the driver JIT-compiles it for the newer card.
rc="$(run_stack 12.0)"
[[ "$rc" == 0 ]] || fail "7: sm_120 run exited $rc: $(tail -20 "$WORK/run.log")"
built || fail "7: a card newer than nvcc got no CUDA build"
[[ "$(cuda_archs)" == "90-virtual" ]] \
    || fail "7: sm_120 on nvcc 12.0 targets '$(cuda_archs)', want 90-virtual (PTX for the driver to JIT)"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm120" ]] || fail "7: sm_120 marker is '$(cat "$MARKER")'"
rc="$(run_stack 12.0)"
[[ "$rc" == 0 ]] || fail "7: sm_120 re-run exited $rc"
built && fail "7: the sm_120 PTX build is redone on every re-run"
# Every nvcc arch at or below the card, and a newer toolkit that knows it.
rc="$(STUB_NVCC_ARCHS="compute_75 compute_80 compute_86 compute_89 compute_90 compute_100 compute_120" run_stack 7.5)"
[[ "$rc" == 0 ]] || fail "7: run exited $rc"
[[ "$(cuda_archs)" == 75 ]] || fail "7: sm_75 targets '$(cuda_archs)', want 75"
# No compute_ list from nvcc, or no compute_cap from the driver: CMake decides.
rc="$(STUB_NVCC_ARCHS=none run_stack 8.6)"
[[ "$rc" == 0 ]] || fail "7: run exited $rc"
[[ "$(cuda_archs)" == native ]] || fail "7: an nvcc without --list-gpu-arch targets '$(cuda_archs)', want native"
rc="$(run_stack '[N/A]')"
[[ "$rc" == 0 ]] || fail "7: run exited $rc"
[[ "$(cuda_archs)" == native ]] || fail "7: unknown compute caps target '$(cuda_archs)', want native"
# A card older than every arch nvcc supports: no clone, no configure, Vulkan
# fallback (which cannot download in this sandbox, hence exit 4).
rc="$(run_stack 3.5)"
built && fail "7: a CUDA build was configured for sm_35, which nvcc 12.0 cannot target"
stub_log_has '^git clone' && fail "7: llama.cpp was cloned for a CUDA build that cannot target the card"
grep -q 'Vulkan' "$WORK/run.log" || fail "7: the sm_35 run did not fall back to the Vulkan prebuilt: $(tail -5 "$WORK/run.log")"

# ── 8. a dead NVIDIA driver is refused, not rebuilt for CPU ──────────────────
# The Aug 2026 outage shape: the card is on the PCI bus, nvidia-smi fails. The
# build selector's VRAM read used to fail too and the run swapped the CUDA
# llama-server for the CPU prebuilt. Now the GPU preflight refuses (exit 7)
# before anything changes.
echo "${LC_VER}-cuda-sm89" > "$MARKER"
: > "$STUB_LOG"
STUB_NVIDIA_SMI_FAIL=9 bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$ROOT" > "$WORK/dead.log" 2>&1; rc=$?
[[ "$rc" == 7 ]] || fail "8: a dead driver did not stop the install with exit 7 (rc=$rc): $(tail -5 "$WORK/dead.log")"
grep -q 'NVIDIA driver not responding' "$WORK/dead.log" || fail "8: the refusal does not say the driver is not responding"
[[ "$(cat "$MARKER")" == "${LC_VER}-cuda-sm89" ]] || fail "8: the CUDA build marker was rewritten to '$(cat "$MARKER")'"
stub_log_has '^(cmake|curl|sudo|systemctl)' && fail "8: the refused run still changed the box: $(cat "$STUB_LOG")"
# A CPU box (no NVIDIA card on the bus) is not gated at all.
STUB_LSPCI="" STUB_NVIDIA_SMI_FAIL=9 bash "$LOCALSTACK_SH" "$USER_NAME" "$HOME_DIR" "$ROOT" > "$WORK/cpu.log" 2>&1; rc=$?
[[ "$rc" == 7 ]] && fail "8: a box with no NVIDIA card was refused by the GPU preflight"
grep -q 'no NVIDIA card on the PCI bus' "$WORK/cpu.log" || fail "8: the CPU-box run did not pass the preflight: $(head -5 "$WORK/cpu.log")"

echo "ALL TESTS PASSED"
exit 0
