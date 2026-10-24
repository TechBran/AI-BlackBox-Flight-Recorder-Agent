#!/usr/bin/env bash
# blackbox.service bounds memory instead of recycling on a request count.
#
# 2026-10-09 audit X06-ops-deploy-01 (rank 70): the unit ran uvicorn with
# --limit-max-requests 100000, which makes uvicorn exit once it has served that
# many requests; systemd then restarted it, so a busy box had an unplanned
# outage (dropped chat/voice/CU sessions, the index rebuild) every few days,
# leak or no leak. The unit now caps memory with MemoryHigh= (throttle) and
# MemoryMax= (hard cap, OOM kill, Restart=always), the orchestrator logs a
# [MEM-WATCHDOG] warning as it nears them, and uvicorn keeps its graceful drain
# (uvicorn owns SIGTERM now: audit O01-core-runtime-01).
#
# Renders the unit with the real render_blackbox_unit() from install.sh
# (extracted via sed, as test_systemd_units_verify.sh does) for a sample root.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "missing $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
for fn in render_blackbox_unit render_override_dropin; do
    sed -n "/^$fn()[[:space:]]*{/,/^}/p" "$INSTALL_SH" >> "$FUNC_FILE"
    grep -q "^$fn()" "$FUNC_FILE" || fail "could not extract $fn() from install.sh"
done
# shellcheck source=/dev/null
source "$FUNC_FILE"

# Globals the sourced render functions read.
# shellcheck disable=SC2034
BLACKBOX_ROOT="$WORK/srv/blackbox_poc"
# shellcheck disable=SC2034
REAL_USER="bbx"

UNIT="$WORK/blackbox.service"
render_blackbox_unit > "$UNIT" 2> "$WORK/render.err" || fail "render_blackbox_unit exited non-zero"
[[ -s "$WORK/render.err" ]] && fail "render_blackbox_unit wrote to stderr: $(cat "$WORK/render.err")"
OVERRIDE="$WORK/override.conf"
render_override_dropin > "$OVERRIDE" || fail "render_override_dropin exited non-zero"

# The ExecStart= value, continuation lines joined.
EXEC="$(sed -n '/^ExecStart=/,/[^\\]$/p' "$UNIT" | tr -d '\\\n')"
[[ "$EXEC" == *"-m uvicorn Orchestrator.app:app"* ]] || fail "ExecStart is not the uvicorn command: $EXEC"

# ── no request-count recycle ──────────────────────────────────────────────────
[[ "$EXEC" == *"--limit-max-requests"* ]] && fail "ExecStart still recycles on a request count: $EXEC"
grep -vE '^[[:space:]]*#' "$INSTALL_SH" | grep -q -- '--limit-max-requests' \
    && fail "install.sh still passes --limit-max-requests somewhere"
grep -vE '^[[:space:]]*#' "$OVERRIDE" | grep -q -- '--limit-max-requests' \
    && fail "the override drop-in passes --limit-max-requests"

# The graceful drain stays.
[[ "$EXEC" == *"--timeout-graceful-shutdown 30"* ]] || fail "ExecStart lost --timeout-graceful-shutdown 30: $EXEC"

# ── memory bounds ─────────────────────────────────────────────────────────────
pct() {  # <key> — the percentage value of <key>= in the unit, or empty
    sed -nE "s/^$1=([0-9]+)%\$/\1/p" "$UNIT"
}
HIGH="$(pct MemoryHigh)"
MAX="$(pct MemoryMax)"
[[ -n "$HIGH" ]] || fail "blackbox.service has no MemoryHigh=<n>% line"
[[ -n "$MAX" ]] || fail "blackbox.service has no MemoryMax=<n>% line (the bound that replaced the recycle)"
(( MAX > HIGH )) || fail "MemoryMax=$MAX% must sit above MemoryHigh=$HIGH%, or the kernel kills before it ever throttles"
(( MAX < 100 )) || fail "MemoryMax=$MAX% leaves the OS no headroom"
[[ "$(grep -c '^MemoryMax=' "$UNIT")" -eq 1 ]] || fail "MemoryMax= is set more than once"
grep -qx 'OOMPolicy=continue' "$UNIT" \
    || fail "OOMPolicy=continue missing: an OOM-killed CLI agent child would stop the whole orchestrator"
grep -qx 'Restart=always' "$UNIT" || fail "Restart=always missing: an OOM kill of uvicorn would leave the service down"

# The override scaffold documents how to change the cap.
grep -qx '# MemoryMax=[0-9]*%' "$OVERRIDE" || fail "the override drop-in does not show how to change MemoryMax"

echo "ALL TESTS PASSED"
exit 0
