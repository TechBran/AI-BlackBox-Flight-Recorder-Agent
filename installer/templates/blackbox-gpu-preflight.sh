#!/usr/bin/env bash
# blackbox-gpu-preflight — refuse to start the on-box model stack while an
# NVIDIA card sits on the PCI bus with its driver dead.
#
# The Aug 7-11 2026 outage: a kernel update booted without a signed NVIDIA
# module, Secure Boot rejected the DKMS build, and for four days the card stayed
# visible to lspci while nvidia-smi failed. llama-swap started anyway, its
# /health said 200, on-box TTS 500'd every synthesis (no CPU path) and STT
# crawled at 10-21 s per utterance on CPU. Nothing said why. This gate turns
# that state into a unit that fails at start, with the reason and the fix in
# the journal. (2026-08-11 plan Track B Tasks 11-12, audit W11.)
#
# Decision table — the same one Orchestrator/hardware.py's probe() applies to
# driver_ok (Orchestrator/tests/gpu_driver_cases.py tests both):
#   no NVIDIA card on the bus (lspci)          -> exit 0 (CPU box)
#   card present, nvidia-smi not installed     -> exit 0 + warning (the box was
#                                                 never set up for the card;
#                                                 the installer built CPU)
#   card present, nvidia-smi -L lists a GPU    -> exit 0
#   card present, nvidia-smi fails, hangs or
#   lists no GPU (after --wait)                -> exit 1  REFUSE
#   lspci not installed                        -> exit 0 + warning (cannot see
#                                                 a card, so cannot judge)
#
# Usage: blackbox-gpu-preflight.sh [--wait SECONDS]
#   --wait N  keep retrying nvidia-smi for up to N seconds before refusing: at
#             boot the module can load after this unit starts (an X server that
#             loses that race never re-probes, but llama-swap can wait).
# Environment:
#   BLACKBOX_GPU_PREFLIGHT=off      run a GPU box CPU-only on purpose (warns,
#                                   exits 0). Set it in a unit drop-in.
#   BLACKBOX_GPU_PREFLIGHT_TIMEOUT  per-call nvidia-smi timeout (default 10 s)
#   BLACKBOX_GPU_PREFLIGHT_POLL     seconds between retries (default 2)
#
# Used by blackbox-models.service (ExecStartPre) and by
# blackbox-install-localstack.sh, which must not rebuild llama-server for CPU
# on a box whose GPU is merely broken. Read-only: runs lspci and nvidia-smi,
# writes nothing.
set -uo pipefail

log() { echo "[gpu-preflight] $*" >&2; }

WAIT_S=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait) WAIT_S="${2:-}"; shift 2 ;;
        *) log "unknown argument: $1"; exit 2 ;;
    esac
done
CALL_TIMEOUT="${BLACKBOX_GPU_PREFLIGHT_TIMEOUT:-10}"
POLL_S="${BLACKBOX_GPU_PREFLIGHT_POLL:-2}"
for _n in "$WAIT_S" "$CALL_TIMEOUT" "$POLL_S"; do
    if [[ ! "$_n" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        log "not a number of seconds: '$_n'"
        exit 2
    fi
done

if ! command -v lspci >/dev/null 2>&1; then
    log "WARNING: lspci is not installed (pciutils), so whether an NVIDIA card is present"
    log "cannot be checked; starting without the GPU gate."
    exit 0
fi
CARD="$(lspci 2>/dev/null | grep -iE 'vga|3d|display' | grep -i nvidia | head -n 1 || true)"
if [[ -z "$CARD" ]]; then
    echo "[gpu-preflight] no NVIDIA card on the PCI bus — CPU box, nothing to check."
    exit 0
fi
CARD="${CARD#*: }"

if ! command -v nvidia-smi >/dev/null 2>&1; then
    log "WARNING: $CARD is on the PCI bus but nvidia-smi is not installed (no NVIDIA"
    log "driver on this box), so the on-box models run on CPU."
    exit 0
fi

if [[ "${BLACKBOX_GPU_PREFLIGHT:-}" == "off" ]]; then
    log "WARNING: BLACKBOX_GPU_PREFLIGHT=off — not checking the NVIDIA driver for $CARD;"
    log "if it is dead the on-box models run on CPU (and on-box TTS fails)."
    exit 0
fi

# Fractional --wait / poll values are allowed; the deadline is whole seconds.
deadline=$(( SECONDS + ${WAIT_S%.*} ))
while :; do
    out="$(timeout "$CALL_TIMEOUT" nvidia-smi -L 2>&1)"; rc=$?
    gpu_line="$(grep -m 1 '^GPU ' <<< "$out" || true)"
    if [[ "$rc" -eq 0 && -n "$gpu_line" ]]; then
        echo "[gpu-preflight] NVIDIA driver OK: $gpu_line"
        exit 0
    fi
    if [[ "$rc" -eq 124 ]]; then
        why="timed out after ${CALL_TIMEOUT}s"
    elif [[ "$rc" -eq 0 ]]; then
        why="exited 0 but listed no GPU"
    else
        said="$(grep -m 1 -v '^[[:space:]]*$' <<< "$out" || true)"
        why="exited $rc${said:+: $said}"
    fi
    (( SECONDS >= deadline )) && break
    sleep "$POLL_S"
done

log "FAIL: NVIDIA driver not responding — $CARD is on the PCI bus but"
log "  'nvidia-smi -L' $why."
log "Refusing to start the on-box model stack on CPU: on-box TTS cannot run"
log "without CUDA and STT/embeddings would crawl, with every status still green."
log "Check:  nvidia-smi;  mokutil --sb-state;  dkms status;  journalctl -k -b | grep -i nvidia"
log "Usual cause: a new kernel booted without a signed NVIDIA module for it."
log "Install the kernel-tracking signed module package for your driver"
log "(linux-modules-nvidia-<driver>-generic) or enrol the DKMS MOK key, then reboot."
log "Then: sudo systemctl reset-failed blackbox-models.service && sudo systemctl start blackbox-models.service"
log "To run this GPU box CPU-only on purpose, set Environment=BLACKBOX_GPU_PREFLIGHT=off"
log "in a blackbox-models.service drop-in."
exit 1
