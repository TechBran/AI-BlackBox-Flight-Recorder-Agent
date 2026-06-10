#!/usr/bin/env bash
# Test for install.sh's setup_asterisk_blackbox_include() function.
#
# install.sh executes its install steps at top level (no main() guard), so
# sourcing it directly would run the whole installer (and require root/sudo).
# Instead we EXTRACT just the function definition via sed into a temp file and
# source that, then run it against a mktemp sandbox with path overrides — never
# touching the real /etc. visudo is skipped via SKIP_VISUDO=1.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

# ── Extract the function into a sourceable temp file ──────────────────────────
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
sed -n '/^setup_asterisk_blackbox_include()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
if ! grep -q 'setup_asterisk_blackbox_include()' "$FUNC_FILE"; then
    fail "could not extract setup_asterisk_blackbox_include() from install.sh"
fi

# ── Sandbox: redirect every system path into the temp dir ─────────────────────
export ASTERISK_ETC="$WORK/etc/asterisk"
export BLACKBOX_D="$ASTERISK_ETC/blackbox.d"
export SYSTEMD_DROPIN_DIR="$WORK/etc/systemd/system/blackbox.service.d"
export SUDOERS_FILE="$WORK/etc/sudoers.d/blackbox-asterisk"
export SERVICE_USER="$USER"          # a real user so chown best-effort succeeds/fails harmlessly
export SKIP_VISUDO=1                  # never run visudo against anything in the test

mkdir -p "$ASTERISK_ETC" "$WORK/etc/sudoers.d"
# Seed fake Asterisk configs (they exist on a real box).
printf '[transport-udp]\ntype=transport\n' > "$ASTERISK_ETC/pjsip.conf"
printf '[from-internal]\nexten => 100,1,Answer()\n'  > "$ASTERISK_ETC/extensions.conf"

# shellcheck source=/dev/null
source "$FUNC_FILE"

# ── Run 1 ─────────────────────────────────────────────────────────────────────
setup_asterisk_blackbox_include >/dev/null 2>&1 || fail "function returned non-zero on first run"

INCLUDE_LINE='#include "blackbox.d/*.conf"'

# Assert: blackbox.d created
[[ -d "$BLACKBOX_D" ]] || fail "blackbox.d directory was not created"

# Assert: include present in both confs
grep -qF "$INCLUDE_LINE" "$ASTERISK_ETC/pjsip.conf" \
    || fail "include line missing from pjsip.conf"
grep -qF "$INCLUDE_LINE" "$ASTERISK_ETC/extensions.conf" \
    || fail "include line missing from extensions.conf"

# Assert: drop-in written with ReadWritePaths line (literal /etc path)
[[ -f "$SYSTEMD_DROPIN_DIR/asterisk.conf" ]] \
    || fail "systemd drop-in asterisk.conf not written"
grep -qF 'ReadWritePaths=/etc/asterisk/blackbox.d' "$SYSTEMD_DROPIN_DIR/asterisk.conf" \
    || fail "ReadWritePaths line missing from drop-in"

# Assert: sudoers file written with the scoped NOPASSWD reload line
[[ -f "$SUDOERS_FILE" ]] || fail "sudoers file not written"
grep -qF 'NOPASSWD:' "$SUDOERS_FILE" || fail "NOPASSWD missing from sudoers file"
grep -qF 'asterisk -rx pjsip reload' "$SUDOERS_FILE" \
    || fail "pjsip reload command missing from sudoers file"
grep -qF 'asterisk -rx dialplan reload' "$SUDOERS_FILE" \
    || fail "dialplan reload command missing from sudoers file"

# ── Run 2 (idempotency) ───────────────────────────────────────────────────────
setup_asterisk_blackbox_include >/dev/null 2>&1 || fail "function returned non-zero on second run"

PJSIP_COUNT=$(grep -cF "$INCLUDE_LINE" "$ASTERISK_ETC/pjsip.conf")
EXT_COUNT=$(grep -cF "$INCLUDE_LINE" "$ASTERISK_ETC/extensions.conf")
[[ "$PJSIP_COUNT" -eq 1 ]] || fail "include duplicated in pjsip.conf (count=$PJSIP_COUNT)"
[[ "$EXT_COUNT"   -eq 1 ]] || fail "include duplicated in extensions.conf (count=$EXT_COUNT)"

echo "ALL TESTS PASSED"
exit 0
