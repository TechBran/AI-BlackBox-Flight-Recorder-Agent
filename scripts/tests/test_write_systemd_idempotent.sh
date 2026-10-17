#!/usr/bin/env bash
# blackbox-write-systemd reports an already-current target without writing it.
#
# The service's startup drift-heal cannot read the root-only 0440 sudoers file
# to compare it, and inside its ProtectSystem=strict namespace /etc is
# read-only, so a dispatch that always wrote logged a failed write on every boot
# for a file that was already right. The helper (running as root) now compares
# bytes, mode and ownership first. Runs unprivileged under the test sandbox
# prefix, like test_write_systemd_targets.sh.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="$HERE/../../installer/templates/blackbox-write-systemd.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }
[[ -f "$WS" ]] || fail "helper not found at $WS"

WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT
SANDBOX="$WORK/root"
STORE="$SANDBOX/etc/blackbox/templates"
mkdir -p "$STORE"
printf '[Unit]\nDescription=BlackBox MCP\n[Service]\nExecStart=/bin/true\n' > "$STORE/mcp-unit"
DEST="$SANDBOX/etc/systemd/system/blackbox-mcp.service"

run() { OUT="$(BLACKBOX_WRITE_SYSTEMD_DEST_ROOT="$SANDBOX" bash "$WS" "$@" 2>&1)"; RC=$?; }

# 1. first dispatch writes the target
run mcp-unit
[[ "$RC" -eq 0 ]] || fail "first run rc=$RC: $OUT"
grep -q 'Wrote ' <<<"$OUT" || fail "first run did not write: $OUT"

# 2. second dispatch: already current, nothing written, even where the
#    destination directory is read-only (the service sandbox's /etc)
chmod 555 "$(dirname "$DEST")"
run mcp-unit
[[ "$RC" -eq 0 ]] || fail "already-current run rc=$RC: $OUT"
grep -q 'already current' <<<"$OUT" || fail "expected 'already current': $OUT"
[[ -e "${DEST}.update-tmp" ]] && fail "already-current run left a .update-tmp"
chmod 755 "$(dirname "$DEST")"

# 3. same bytes but the wrong mode is NOT current: the helper rewrites it
chmod 600 "$DEST"
run mcp-unit
[[ "$RC" -eq 0 ]] || fail "mode-drift run rc=$RC: $OUT"
grep -q 'Wrote ' <<<"$OUT" || fail "mode drift was not rewritten: $OUT"
[[ "$(stat -c %a "$DEST")" == "644" ]] || fail "mode not restored: $(stat -c %a "$DEST")"

# 4. changed store content is NOT current either
printf '[Unit]\nDescription=changed\n' > "$STORE/mcp-unit"
run mcp-unit
grep -q 'Wrote ' <<<"$OUT" || fail "content drift was not rewritten: $OUT"
cmp -s "$STORE/mcp-unit" "$DEST" || fail "dest does not match the changed store entry"

echo "PASS: blackbox-write-systemd idempotency (already-current, mode drift, content drift)"
