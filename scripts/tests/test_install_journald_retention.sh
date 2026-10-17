#!/usr/bin/env bash
# Test for install.sh's journald retention + journal-access steps.
#
# 2026-10-09 audit W16 / X06-ops-deploy-05 (rank 12): blackbox.service logs only
# to the journal, yet nothing configured journald, so the distro default
# (10 % of the filesystem, capped at 4 GiB) was the whole evidence horizon —
# MS02 lost its Aug 7-12 outage record that way, and dev's is rolling off. The
# only retention install.sh shipped was a logrotate rule for /var/log/blackbox,
# a directory nothing writes. Separately, rank 17 replaces the root
# `journalctl -u blackbox.service *` sudo grant with systemd-journal membership.
#
# Proves:
#   1. the SystemMaxUse sizing is 10 % of the journal filesystem, clamped to
#      [1 GiB, 16 GiB] (larger than the old 4 GiB default horizon on any real
#      disk, never below 1 GiB, and garbage input never fails the installer);
#   2. install_journald_retention writes a drop-in with Storage=persistent and
#      that SystemMaxUse, and NO MaxRetentionSec (a ceiling that deletes old
#      entries, not a floor);
#   3. it is idempotent and never restarts journald in the sandbox;
#   4. install.sh runs both steps before Step 1's apt phase, adds the service
#      user to systemd-journal, and retires the dead logrotate rule.
#
# install.sh has no main() guard, so the functions are EXTRACTED via sed and
# sourced, then run against a mktemp sandbox — never touching /etc or journald.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
for fn in journald_system_max_use_mb install_journald_retention; do
    sed -n "/^${fn}()[[:space:]]*{/,/^}/p" "$INSTALL_SH" >> "$FUNC_FILE"
    grep -q "^${fn}()" "$FUNC_FILE" || fail "could not extract ${fn}() from install.sh"
done
# shellcheck source=/dev/null
source "$FUNC_FILE"

# ── 1. sizing ─────────────────────────────────────────────────────────────────
expect_size() {  # <fs-KiB> <expected-MiB>
    local got
    got="$(journald_system_max_use_mb "$1")"
    [[ "$got" == "$2" ]] || fail "journald_system_max_use_mb $1 = '$got', expected $2"
}
expect_size $((8 * 1024 * 1024))          1024    # 8 GiB disk: 10 % < floor
expect_size $((64 * 1024 * 1024))         6553    # 64 GiB: 10 %
expect_size $((100 * 1024 * 1024))        10240   # 100 GiB: 10 %
expect_size $((1920 * 1024 * 1024))       16384   # 1.8 TiB (dev box): capped
expect_size 0                             1024
expect_size ""                            1024
expect_size "not-a-number"                1024

# ── 2. the drop-in ────────────────────────────────────────────────────────────
mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >> "${FAKE_SYSTEMCTL_LOG:?}"
exit 0
FAKE
chmod +x "$WORK/bin/systemctl"
export FAKE_SYSTEMCTL_LOG="$WORK/systemctl.log"
: > "$FAKE_SYSTEMCTL_LOG"

DROPIN_DIR="$WORK/etc/systemd/journald.conf.d"
DROPIN="$DROPIN_DIR/blackbox.conf"
FS_KB="$(df -Pk "$WORK" | awk 'NR==2 {print $2}')"
WANT_MB="$(journald_system_max_use_mb "$FS_KB")"

run_install() {
    PATH="$WORK/bin:$PATH" JOURNALD_DROPIN_DIR="$DROPIN_DIR" JOURNAL_FS_PATH="$WORK" \
        INSTALL_SUDO="" install_journald_retention >"$WORK/run.log" 2>&1
}
run_install || fail "install_journald_retention returned non-zero: $(cat "$WORK/run.log")"
[[ -f "$DROPIN" ]] || fail "drop-in not written at $DROPIN"
[[ "$(stat -c '%a' "$DROPIN")" == "644" ]] || fail "drop-in mode is $(stat -c '%a' "$DROPIN"), expected 644"
grep -qx '\[Journal\]' "$DROPIN" || fail "drop-in has no [Journal] section"
grep -qx 'Storage=persistent' "$DROPIN" || fail "drop-in does not set Storage=persistent"
grep -qx "SystemMaxUse=${WANT_MB}M" "$DROPIN" || fail "drop-in does not set SystemMaxUse=${WANT_MB}M: $(cat "$DROPIN")"
grep -qE '^[[:space:]]*MaxRetentionSec=' "$DROPIN" \
    && fail "drop-in sets MaxRetentionSec — that is a CEILING that deletes evidence, not a retention floor"
[[ "$(grep -cE '^[A-Za-z]+=' "$DROPIN")" -eq 2 ]] \
    || fail "drop-in sets more than Storage= and SystemMaxUse=: $(grep -E '^[A-Za-z]+=' "$DROPIN")"
grep -q 'restart' "$FAKE_SYSTEMCTL_LOG" && fail "sandbox run restarted systemd-journald"

# ── 3. idempotent ─────────────────────────────────────────────────────────────
cp "$DROPIN" "$WORK/first.conf"
run_install || fail "second install_journald_retention returned non-zero"
cmp -s "$DROPIN" "$WORK/first.conf" || fail "second run changed the drop-in"
grep -qi 'already current' "$WORK/run.log" || fail "second run did not report the drop-in as already current"

# ── 4. install.sh wiring (source-level) ───────────────────────────────────────
APT_LINE="$(grep -nE '^[[:space:]]*\| xargs sudo apt install -y' "$INSTALL_SH" | head -1 | cut -d: -f1)"
[[ -n "$APT_LINE" ]] || fail "could not find Step 1's apt install line"
for call in install_journald_retention grant_journal_read_access; do
    line="$(grep -n "^${call}\$" "$INSTALL_SH" | head -1 | cut -d: -f1)"
    [[ -n "$line" ]] || fail "install.sh never calls $call"
    [[ "$line" -lt "$APT_LINE" ]] \
        || fail "$call runs at line $line, after Step 1's apt (line $APT_LINE) — a failing apt step would strand it"
done
grep -qE '^[[:space:]]+sudo systemctl restart systemd-journald' "$INSTALL_SH" \
    || fail "install.sh no longer restarts systemd-journald after writing the drop-in"
grep -qE '^[[:space:]]*if \[\[ "\$JOURNALD_DROPIN_DIR" == "/etc/systemd/journald.conf.d" \]\]' "$INSTALL_SH" \
    || fail "install_journald_retention no longer pins the production drop-in dir"
grep -qE '^[^#]*usermod -aG systemd-journal "\$REAL_USER"' "$INSTALL_SH" \
    || fail "install.sh does not add the service user to systemd-journal"
grep -qE '^[^#]*tee /etc/logrotate\.d/blackbox' "$INSTALL_SH" \
    && fail "install.sh still writes the dead /var/log/blackbox logrotate rule"
grep -qE '^[^#]*rm -f /etc/logrotate\.d/blackbox' "$INSTALL_SH" \
    || fail "install.sh does not retire a previously installed /var/log/blackbox logrotate rule"

echo "ALL TESTS PASSED"
exit 0
