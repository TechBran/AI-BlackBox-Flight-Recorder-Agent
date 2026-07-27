#!/usr/bin/env bash
# Behaviour test for blackbox-write-systemd's NO-SOURCE, store-backed dispatch.
#
# install.sh-grant removal, 2026-07-27 (trusted-template-store design): the
# helper no longer takes a caller-supplied <source_file>. For a whitelisted
# <target_kind> it copies the ROOT-OWNED /etc/blackbox/templates/<kind> to that
# kind's hardcoded destination. This exercises the ACTUAL template script
# unprivileged, against a mktemp tree, via the TEST-ONLY
# $BLACKBOX_WRITE_SYSTEMD_DEST_ROOT sandbox prefix (empty in production, where
# sudo's env_reset scrubs it). That single prefix rebases BOTH the store read
# AND the destination write into the sandbox, so the test populates a fake store
# and asserts the copy.
#
# It proves: a systemd kind lands 0644 from the store; a plain kind lands 0644;
# the visudo-gated asterisk-sudoers kind lands 0440 on valid store content and
# refuses (exit 5) broken sudoers; a SECOND argument is ignored (the source of
# truth is the store, never a caller file); a MISSING store entry fails closed
# (exit 6); and an unknown kind is still rejected (exit 4).
#
# The destination AND the store source are both hardcoded/fixed in the helper;
# this test supplies only a kind — never a path, never content — mirroring the
# sudo grant's actual call shape.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WS="$REPO_ROOT/installer/templates/blackbox-write-systemd.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$WS" ]] || fail "blackbox-write-systemd.sh not found at $WS"
bash -n "$WS" || fail "blackbox-write-systemd.sh does not parse"

# visudo is what the sudoers arm shells out to; skip the visudo-gated cases if
# it is somehow absent rather than report a spurious failure.
HAVE_VISUDO=0
[[ -x /usr/sbin/visudo ]] && HAVE_VISUDO=1

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SANDBOX="$WORK/root"
STORE="$SANDBOX/etc/blackbox/templates"
mkdir -p "$STORE"

# Populate a fake root-owned store entry for <kind> with given content.
store() {  # <kind> <content>
    printf '%s' "$2" > "$STORE/$1"
}

run() {  # <target_kind> [extra-arg...] ; sets global RC
    BLACKBOX_WRITE_SYSTEMD_DEST_ROOT="$SANDBOX" bash "$WS" "$@" >/dev/null 2>&1
    RC=$?
}

# ── 1. a systemd kind (mcp-unit) copies the STORE entry to its hardcoded dest ─
STORE_CONTENT_UNIT='[Unit]
Description=BlackBox MCP
[Service]
ExecStart=/bin/true
'
store mcp-unit "$STORE_CONTENT_UNIT"
run mcp-unit
[[ "$RC" -eq 0 ]] || fail "mcp-unit returned $RC, expected 0"
DEST_UNIT="$SANDBOX/etc/systemd/system/blackbox-mcp.service"
[[ -f "$DEST_UNIT" ]] || fail "mcp-unit did not write $DEST_UNIT"
diff -q "$STORE/mcp-unit" "$DEST_UNIT" >/dev/null \
    || fail "mcp-unit content does not match the STORE entry"
[[ "$(stat -c '%a' "$DEST_UNIT")" == "644" ]] \
    || fail "mcp-unit mode is $(stat -c '%a' "$DEST_UNIT"), expected 644"
# No .update-tmp left behind (atomic rename completed).
[[ -e "${DEST_UNIT}.update-tmp" ]] && fail "mcp-unit left a .update-tmp behind"

# ── 2. a SECOND argument is IGNORED — the store is still the only source ───────
# Legacy callers (runner / startup hooks) pass a rendered source as $2. It must
# never be read: the write must still equal the STORE entry, not the arg file.
ATTACKER="$WORK/attacker.service"
printf '[Service]\nUser=root\nExecStartPre=/bin/touch /pwned\n' > "$ATTACKER"
rm -f "$DEST_UNIT"
run mcp-unit "$ATTACKER"
[[ "$RC" -eq 0 ]] || fail "mcp-unit with a 2nd arg returned $RC, expected 0 (arg ignored)"
diff -q "$STORE/mcp-unit" "$DEST_UNIT" >/dev/null \
    || fail "mcp-unit wrote the caller's 2nd-arg file instead of the STORE entry — SOURCE LEAK"
grep -q 'ExecStartPre' "$DEST_UNIT" \
    && fail "the attacker 2nd-arg content reached the deployed unit — SOURCE LEAK"

# ── 3. logrotate is a plain 0644 file (no daemon-reload path) ─────────────────
store logrotate '/var/log/blackbox/*.log {
  daily
  rotate 7
}
'
run logrotate
[[ "$RC" -eq 0 ]] || fail "logrotate returned $RC, expected 0"
DEST_LR="$SANDBOX/etc/logrotate.d/blackbox"
[[ -f "$DEST_LR" ]] || fail "logrotate did not write $DEST_LR"
[[ "$(stat -c '%a' "$DEST_LR")" == "644" ]] \
    || fail "logrotate mode is $(stat -c '%a' "$DEST_LR"), expected 644"

# ── 4. a MISSING store entry fails closed (exit 6), writes nothing ────────────
# ydotoold-unit has no store entry populated here (it is an install.sh-only
# inline-heredoc kind). Dispatching it must refuse rather than fabricate.
DEST_YD="$SANDBOX/etc/systemd/system/ydotoold.service"
run ydotoold-unit
[[ "$RC" -eq 6 ]] || fail "missing store entry returned $RC, expected 6 (fail closed)"
[[ -e "$DEST_YD" ]] && fail "a kind with no store entry still wrote $DEST_YD"

if [[ "$HAVE_VISUDO" -eq 1 ]]; then
    # ── 5. asterisk-sudoers: valid store content lands at 0440 via visudo -c ──
    store asterisk-sudoers 'bbx ALL=(root) NOPASSWD: /usr/bin/asterisk -rx *
'
    run asterisk-sudoers
    [[ "$RC" -eq 0 ]] || fail "asterisk-sudoers (valid) returned $RC, expected 0"
    DEST_ASU="$SANDBOX/etc/sudoers.d/blackbox-asterisk"
    [[ -f "$DEST_ASU" ]] || fail "asterisk-sudoers did not write $DEST_ASU"
    [[ "$(stat -c '%a' "$DEST_ASU")" == "440" ]] \
        || fail "asterisk-sudoers mode is $(stat -c '%a' "$DEST_ASU"), expected 440"

    # ── 6. asterisk-sudoers: a syntactically broken STORE entry is REFUSED ────
    # The whole reason the sudoers arm shells out to visudo -c: a bad rule must
    # never reach /etc/sudoers.d, where it could brick sudo or install a grant.
    rm -f "$DEST_ASU"
    store asterisk-sudoers 'this is not a valid sudoers line @@@!!!
'
    run asterisk-sudoers
    [[ "$RC" -eq 5 ]] || fail "asterisk-sudoers (broken) returned $RC, expected 5 (visudo -c refusal)"
    [[ -e "$DEST_ASU" ]] && fail "a broken sudoers store entry was still written to $DEST_ASU"
else
    echo "NOTE: /usr/sbin/visudo absent — skipped asterisk-sudoers cases"
fi

# ── 7. an unknown kind is still rejected (exit 4), before any store lookup ────
run bogus-not-a-kind
[[ "$RC" -eq 4 ]] || fail "unknown target_kind returned $RC, expected 4"

# ── 8. a bare invocation with no kind is a usage error (exit 2) ───────────────
run
[[ "$RC" -eq 2 ]] || fail "no target_kind returned $RC, expected 2"

echo "ALL TESTS PASSED"
exit 0
