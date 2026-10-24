#!/usr/bin/env bash
# Tests for the reranker provisioning scripts install.sh runs on every re-run
# (Steps 2d and 2e).
#
# 2026-10-09 audit X06-ops-deploy-06 (rank 41): blackbox-install-reranker.sh ran
# `pip install --upgrade vllm` (whatever PyPI served that day) and
# `systemctl enable` + restart of vllm-reranker.service on every GPU box, so an
# update re-run resurrected the reranker the local-model-stack migration had
# retired and re-pinned its VRAM beside llama-swap. The CPU sibling installed
# unpinned torch and sentence-transformers into the Orchestrator venv.
#
# Proves, running the real scripts in a mktemp sandbox (PATH stubs for sudo,
# systemctl, nvidia-smi, curl and the venv tools; nothing under /etc touched):
#   1. a GPU box that did not ask for vLLM gets no venv, no pip, no unit;
#   2. an explicit request (RERANK_PROVIDER=vllm, or --vllm) is still refused
#      while blackbox-models.service (llama-swap) is enabled;
#   3. an explicit request on a box without llama-swap installs a PINNED vllm
#      (no --upgrade) and enables the unit — the path is kept, not deleted;
#   4. a REAL_HOME the unit files cannot carry is refused before any change;
#   5. the CPU path installs exact torch / sentence-transformers / transformers
#      versions;
#   6. run in install.sh's order (Step 2d, then Step 2f's localstack script),
#      the two GPU reranker owners never end up enabled together: an explicit
#      vLLM request on a fresh box, a re-run after the operator chose vLLM, and
#      a box where an old run left both enabled all keep blackbox-models.service
#      from being enabled or restarted beside vllm-reranker.service.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RERANKER_SH="$REPO_ROOT/installer/templates/blackbox-install-reranker.sh"
CPU_SH="$REPO_ROOT/installer/templates/blackbox-install-reranker-cpu.sh"
# shellcheck source=scripts/tests/lib/installer_stubs.sh
source "$REPO_ROOT/scripts/tests/lib/installer_stubs.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$RERANKER_SH" ]] || fail "missing $RERANKER_SH"
[[ -f "$CPU_SH" ]] || fail "missing $CPU_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
stub_kit_init "$WORK"

USER_NAME="$(id -un)"
ROOT="$WORK/root"
mkdir -p "$ROOT/installer/templates"
cp "$REPO_ROOT/installer/templates/vllm-reranker.service" "$ROOT/installer/templates/"

# run_gpu <case> [args...] — fresh home per case; prints the exit code.
run_gpu() {
    local case_dir="$WORK/$1"; shift
    mkdir -p "$case_dir/home"
    : > "$STUB_LOG"
    bash "$RERANKER_SH" "$USER_NAME" "$case_dir/home" "$ROOT" "$@" > "$case_dir/run.log" 2>&1
    echo $?
}
no_provisioning() {  # <case> — nothing was installed or enabled
    stub_log_has 'pip\[' && fail "$1: pip ran: $(grep 'pip\[' "$STUB_LOG")"
    stub_log_has 'python -m venv' && fail "$1: a rerank venv was created"
    stub_log_has 'systemctl (enable|restart)' && fail "$1: systemctl enabled/restarted something: $(grep systemctl "$STUB_LOG")"
    [[ -e "$STUB_ROOT/etc/systemd/system/vllm-reranker.service" ]] && fail "$1: the vllm-reranker unit was written"
    return 0
}

# ── 1. GPU box, not requested ─────────────────────────────────────────────────
rc="$(RERANK_PROVIDER='' STUB_ENABLED_UNITS="" run_gpu not-requested)"
[[ "$rc" == 0 ]] || fail "1: not-requested run exited $rc: $(cat "$WORK/not-requested/run.log")"
no_provisioning "1 (not requested)"
grep -q 'RERANK_PROVIDER=vllm' "$WORK/not-requested/run.log" \
    || fail "1: the skip message does not say how to request the vLLM reranker"

# ── 2. requested, but llama-swap owns the GPU ────────────────────────────────
rc="$(RERANK_PROVIDER=vllm STUB_ENABLED_UNITS="blackbox-models.service" run_gpu with-llama-swap)"
[[ "$rc" == 0 ]] || fail "2: run exited $rc: $(cat "$WORK/with-llama-swap/run.log")"
no_provisioning "2 (llama-swap enabled)"
grep -q 'blackbox-models.service' "$WORK/with-llama-swap/run.log" \
    || fail "2: the skip message does not name blackbox-models.service"
rc="$(RERANK_PROVIDER='' STUB_ENABLED_UNITS="blackbox-models.service" run_gpu flag-with-llama-swap --vllm)"
[[ "$rc" == 0 ]] || fail "2: --vllm run exited $rc"
no_provisioning "2 (--vllm, llama-swap enabled)"

# ── 3. requested, no llama-swap: pinned install, unit enabled ────────────────
check_provisioned() {  # <case>
    local c="$1"
    stub_log_has "pip\[$WORK/$c/home/rerank-venv\] install vllm==[0-9]+\.[0-9]+\.[0-9]+$" \
        || fail "3 ($c): vllm was not installed at an exact version: $(grep 'pip\[' "$STUB_LOG")"
    stub_log_has 'pip\[.*--upgrade.*vllm|pip\[.*install vllm$' \
        && fail "3 ($c): vllm installed unpinned or with --upgrade"
    stub_log_has 'systemctl enable vllm-reranker.service' || fail "3 ($c): unit not enabled"
    [[ -f "$STUB_ROOT/etc/systemd/system/vllm-reranker.service" ]] || fail "3 ($c): unit not written"
    grep -q "^ExecStart=$WORK/$c/home/start-reranker.sh$" "$STUB_ROOT/etc/systemd/system/vllm-reranker.service" \
        || fail "3 ($c): unit ExecStart not rendered with the real home"
    [[ -x "$WORK/$c/home/start-reranker.sh" ]] || fail "3 ($c): start script not installed"
    rm -f "$STUB_ROOT/etc/systemd/system/vllm-reranker.service"
}
rc="$(RERANK_PROVIDER=vllm STUB_ENABLED_UNITS="" run_gpu requested)"
[[ "$rc" == 0 ]] || fail "3: requested run exited $rc: $(cat "$WORK/requested/run.log")"
check_provisioned requested
rc="$(RERANK_PROVIDER='' STUB_ENABLED_UNITS="" run_gpu flag --vllm)"
[[ "$rc" == 0 ]] || fail "3: --vllm run exited $rc: $(cat "$WORK/flag/run.log")"
check_provisioned flag

# ── 4. a home the unit cannot carry is refused up front ──────────────────────
: > "$STUB_LOG"
RERANK_PROVIDER=vllm bash "$RERANKER_SH" "$USER_NAME" "$WORK/bad home" "$ROOT" > "$WORK/bad.log" 2>&1; rc=$?
[[ "$rc" == 2 ]] || fail "4: a home with a space was not refused with exit 2 (rc=$rc): $(cat "$WORK/bad.log")"
[[ -s "$STUB_LOG" ]] && fail "4: the script ran commands before refusing the home: $(cat "$STUB_LOG")"

# ── 5. CPU path pins ──────────────────────────────────────────────────────────
CPU_ROOT="$WORK/cpu-root"
mkdir -p "$CPU_ROOT/Orchestrator/venv/bin"
cat > "$CPU_ROOT/Orchestrator/venv/bin/pip" <<'EOF'
#!/usr/bin/env bash
echo "orch-pip $*" >> "$STUB_LOG"
EOF
cat > "$CPU_ROOT/Orchestrator/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
echo "orch-python $*" >> "$STUB_LOG"
case "$*" in *probe*) echo "MID 0 32768" ;; esac
EOF
chmod +x "$CPU_ROOT/Orchestrator/venv/bin/"*
: > "$STUB_LOG"
bash "$CPU_SH" "$USER_NAME" "$WORK/cpu-home" "$CPU_ROOT" > "$WORK/cpu.log" 2>&1; rc=$?
[[ "$rc" == 0 ]] || fail "5: CPU reranker script exited $rc: $(cat "$WORK/cpu.log")"
# Both pins resolve under the lock's constraints (-c requirements.txt, audit U14).
stub_log_has '^orch-pip install torch==[0-9]+\.[0-9]+\.[0-9]+ --index-url https://download\.pytorch\.org/whl/cpu -c .*/requirements\.txt$' \
    || fail "5: CPU torch is not installed at an exact version: $(grep orch-pip "$STUB_LOG")"
stub_log_has '^orch-pip install sentence-transformers==[0-9]+\.[0-9]+\.[0-9]+ transformers==[0-9]+\.[0-9]+\.[0-9]+ -c .*/requirements\.txt$' \
    || fail "5: sentence-transformers/transformers are not installed at exact versions: $(grep orch-pip "$STUB_LOG")"

# ── 6. Step 2d then Step 2f, in install.sh order ─────────────────────────────
# blackbox-install-reranker.sh refuses while blackbox-models.service is enabled,
# but install.sh runs it BEFORE the localstack script, which enables and
# restarts blackbox-models.service. The stub systemctl keeps what `enable`
# records (in $STUB_UNITS), so each step sees what the previous one left.
LOCALSTACK_SH="$REPO_ROOT/installer/templates/blackbox-install-localstack.sh"
[[ -f "$LOCALSTACK_SH" ]] || fail "missing $LOCALSTACK_SH"
for f in llama-swap-version llamacpp-version llama-swap-config.yaml.template blackbox-models.service \
         blackbox-gpu-preflight.sh; do
    cp "$REPO_ROOT/installer/templates/$f" "$ROOT/installer/templates/"
done
# Step 5's config renderer (Orchestrator/localstack_render.py, stdlib only).
mkdir -p "$ROOT/Orchestrator"
cp "$REPO_ROOT/Orchestrator/localstack_render.py" "$ROOT/Orchestrator/"
LS_VER="$(grep -vE '^[[:space:]]*(#|$)' "$ROOT/installer/templates/llama-swap-version" | head -n1 | tr -d '[:space:]')"
SEQ_HOME="$WORK/seq/home"
mkdir -p "$SEQ_HOME/.blackbox/localstack/bin"
# llama-swap already at the pin, so the localstack run never reaches for the network.
printf '#!/bin/sh\necho "version: %s (stub)"\n' "$LS_VER" > "$SEQ_HOME/.blackbox/localstack/bin/llama-swap"
chmod +x "$SEQ_HOME/.blackbox/localstack/bin/llama-swap"

step() {  # <2d|2f> <RERANK_PROVIDER> <label> — runs one install.sh step; fails on non-zero
    local script="$RERANKER_SH"
    [[ "$1" == 2f ]] && script="$LOCALSTACK_SH"
    RERANK_PROVIDER="$2" STUB_ENABLED_UNITS="" STUB_COMPUTE_CAP=8.9 \
        bash "$script" "$USER_NAME" "$SEQ_HOME" "$ROOT" > "$WORK/seq-$3.log" 2>&1 \
        || fail "6 ($3): step $1 exited $?: $(tail -20 "$WORK/seq-$3.log")"
}
unit_enabled() { grep -qxF "$1" "$STUB_UNITS"; }
one_owner() {  # <label>
    if unit_enabled vllm-reranker.service && unit_enabled blackbox-models.service; then
        fail "6 ($1): vllm-reranker.service and blackbox-models.service both ended up enabled on one GPU"
    fi
}
models_left_alone() {  # <label> — the last step neither enabled nor (re)started llama-swap
    stub_log_has '^systemctl (enable|restart) blackbox-models\.service' \
        && fail "6 ($1): Step 2f enabled/restarted blackbox-models.service beside the vLLM reranker: $(grep '^systemctl' "$STUB_LOG")"
    grep -q 'vllm-reranker.service' "$WORK/seq-$1.log" \
        || fail "6 ($1): Step 2f did not say why it left blackbox-models.service alone"
}

# (a) `RERANK_PROVIDER=vllm bash Scripts/install.sh` on a fresh GPU box: the
#     explicit vLLM request wins, llama-swap is provisioned but not enabled.
: > "$STUB_UNITS"; : > "$STUB_LOG"
step 2d vllm fresh-vllm-2d
unit_enabled vllm-reranker.service || fail "6 (fresh-vllm): the requested vLLM reranker was not enabled"
: > "$STUB_LOG"
step 2f vllm fresh-vllm-2f
one_owner fresh-vllm
models_left_alone fresh-vllm-2f
[[ -f "$SEQ_HOME/.blackbox/localstack/llama-swap-config.yaml" ]] \
    || fail "6 (fresh-vllm): the local stack was not provisioned (switching back should be one command)"

# (b) the default install, then the documented opt-in, then a plain re-run.
: > "$STUB_UNITS"; : > "$STUB_LOG"
step 2d '' default-2d
step 2f '' default-2f
unit_enabled blackbox-models.service || fail "6 (default): a default install did not enable blackbox-models.service"
unit_enabled vllm-reranker.service && fail "6 (default): a default install enabled the vLLM reranker"
systemctl disable --now blackbox-models.service
step 2d vllm optin-2d
unit_enabled vllm-reranker.service || fail "6 (opt-in): the documented opt-in did not enable the vLLM reranker"
: > "$STUB_LOG"
step 2d '' rerun-2d
step 2f '' rerun-2f
one_owner rerun
models_left_alone rerun-2f
unit_enabled vllm-reranker.service || fail "6 (re-run): a plain re-run dropped the operator's vLLM choice"

# (c) a box an old run left with both enabled: nothing is restarted, the
#     operator is told to pick one.
printf '%s\n' vllm-reranker.service blackbox-models.service > "$STUB_UNITS"
: > "$STUB_LOG"
step 2f '' both-2f
models_left_alone both-2f
grep -qi 'warning.*both\|both.*enabled' "$WORK/seq-both-2f.log" \
    || fail "6 (both): a box with both units enabled was not warned: $(cat "$WORK/seq-both-2f.log")"
grep -q 'systemctl disable --now vllm-reranker.service' "$WORK/seq-both-2f.log" \
    || fail "6 (both): the warning does not give the command to return to the local stack"

echo "ALL TESTS PASSED"
exit 0
