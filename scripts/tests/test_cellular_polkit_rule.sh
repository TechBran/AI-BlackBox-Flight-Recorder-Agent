#!/usr/bin/env bash
# Test for Scripts/setup-cellular-polkit.sh and install.sh's re-run migration.
#
# 2026-10-09 audit X06-ops-deploy-09 / O22-06 (rank 17): the opt-in cellular
# polkit rule returned polkit.Result.YES for org.freedesktop.systemd1.manage-units
# (start/stop ANY unit, and transient units: `systemd-run` as root from any code
# in the service) and for every org.freedesktop.NetworkManager.* action. That is
# an in-service→root path around the bounded sudoers perimeter. The in-service
# cellular code only ever deactivates a connection (internet_manager.py
# deep_reconnect: `nmcli connection down`), which is NetworkManager
# network-control; Scripts/auto-apn.sh changes APNs as root and needs no rule.
#
# Proves:
#   1. the generated rule names exactly ONE action, network-control — no
#      systemd1, no NetworkManager prefix/regex match;
#   2. it targets the requested user, parses as JavaScript (when node exists),
#      and the polkit restart is skipped in the sandbox;
#   3. an empty `systemctl show … -p User` result no longer yields a rule for
#      user "" (the old `$(… || id -un)` fallback never fired);
#   4. an invalid user or root is refused and no rule is written;
#   5. install.sh's migration rewrites an already-installed (broad) rule, leaves
#      a box without the rule alone (the rule stays opt-in), and removes the rule
#      rather than keep a broad one when the rewrite fails.
#
# Hermetic: POLKIT_RULES_DIR redirects every write into a mktemp sandbox and a
# fake systemctl on PATH records calls — nothing touches /etc or polkit.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/Scripts/setup-cellular-polkit.sh"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
RULE_NAME="10-blackbox-networkmanager.rules"
ONLY_ACTION="org.freedesktop.NetworkManager.network-control"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$SCRIPT" ]] || fail "setup-cellular-polkit.sh not found at $SCRIPT"
[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"
bash -n "$SCRIPT" || fail "setup-cellular-polkit.sh does not parse"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Fake systemctl: `show … -p User --value` prints $FAKE_UNIT_USER (empty by
# default, i.e. "no such unit / no User="), every call is recorded.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >> "${FAKE_SYSTEMCTL_LOG:?}"
if [[ "${1:-}" == "show" ]]; then
    printf '%s\n' "${FAKE_UNIT_USER:-}"
fi
exit 0
FAKE
chmod +x "$WORK/bin/systemctl"
export FAKE_SYSTEMCTL_LOG="$WORK/systemctl.log"
: > "$FAKE_SYSTEMCTL_LOG"

run_setup() {  # <rules-dir> [user-arg...] ; sets RC
    PATH="$WORK/bin:$PATH" POLKIT_RULES_DIR="$1" bash "$SCRIPT" "${@:2}" >"$WORK/out.log" 2>&1
    RC=$?
}

assert_tight_rule() {  # <rule-file> <expected-user>
    local rule="$1" user="$2"
    [[ -f "$rule" ]] || fail "rule not written at $rule"
    grep -q 'systemd1' "$rule" && fail "rule still references org.freedesktop.systemd1 (manage-units = root via systemd-run)"
    grep -q 'manage-units' "$rule" && fail "rule still grants manage-units"
    grep -qE 'indexOf|startsWith|match\(|RegExp|lookup\(' "$rule" \
        && fail "rule matches action ids by prefix/regex instead of one exact id"
    local ids
    ids="$(grep -oE 'action\.id[[:space:]]*===?[[:space:]]*"[^"]*"' "$rule" | sed -E 's/.*"([^"]*)"/\1/' | sort -u)"
    [[ "$ids" == "$ONLY_ACTION" ]] \
        || fail "rule must grant exactly $ONLY_ACTION; grants: [$(echo $ids)]"
    grep -qF "subject.user !== \"$user\"" "$rule" \
        || fail "rule does not target user '$user'"
    [[ "$(grep -c 'polkit.Result.YES' "$rule")" -eq 1 ]] \
        || fail "rule must have exactly one polkit.Result.YES"
    if command -v node >/dev/null 2>&1; then
        cp "$rule" "$WORK/rule-check.js"
        node --check "$WORK/rule-check.js" 2>"$WORK/node.err" \
            || fail "rule is not valid JavaScript: $(cat "$WORK/node.err")"
    fi
}

# ── 1+2. explicit user → one exact action, sandbox skips the polkit restart ───
R1="$WORK/r1/rules.d"
run_setup "$R1" bbx
[[ "$RC" -eq 0 ]] || fail "setup returned $RC for user bbx: $(cat "$WORK/out.log")"
assert_tight_rule "$R1/$RULE_NAME" bbx
mode="$(stat -c '%a' "$R1/$RULE_NAME")"
[[ "$mode" == "644" ]] || fail "rule mode is $mode, expected 644"
grep -q 'restart' "$FAKE_SYSTEMCTL_LOG" && fail "sandbox run restarted polkit"

# ── 3. no arg, unit reports no User= → falls back past the empty string ───────
R2="$WORK/r2/rules.d"
FAKE_UNIT_USER="" SUDO_USER="svcuser" run_setup "$R2"
[[ "$RC" -eq 0 ]] || fail "setup returned $RC with empty unit User=: $(cat "$WORK/out.log")"
grep -q 'subject.user !== ""' "$R2/$RULE_NAME" && fail "empty unit User= produced a rule for user \"\""
assert_tight_rule "$R2/$RULE_NAME" svcuser

# The unit's User= wins over SUDO_USER when present.
R3="$WORK/r3/rules.d"
FAKE_UNIT_USER="unituser" SUDO_USER="svcuser" run_setup "$R3"
[[ "$RC" -eq 0 ]] || fail "setup returned $RC with unit User=unituser"
assert_tight_rule "$R3/$RULE_NAME" unituser

# ── 4. hostile / meaningless users are refused, nothing written ───────────────
for bad in 'bad"user' 'a); polkit.Result.YES; (' 'Root' 'root' ' ' '-x'; do
    RB="$WORK/bad-$RANDOM/rules.d"
    run_setup "$RB" "$bad"
    [[ "$RC" -ne 0 ]] || fail "setup accepted service user [$bad]"
    [[ -e "$RB/$RULE_NAME" ]] && fail "setup wrote a rule for refused user [$bad]"
done

# ── 5. install.sh migration ───────────────────────────────────────────────────
FUNC_FILE="$WORK/func.sh"
sed -n '/^rewrite_installed_cellular_polkit_rule()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
grep -q 'rewrite_installed_cellular_polkit_rule()' "$FUNC_FILE" \
    || fail "could not extract rewrite_installed_cellular_polkit_rule() from install.sh"
grep -qE '^rewrite_installed_cellular_polkit_rule$' "$INSTALL_SH" \
    || fail "install.sh never calls rewrite_installed_cellular_polkit_rule"
# It must run before Step 1's apt phase: under set -e a later failure must not
# strand the broad rule on a box that re-runs the installer to close it.
CALL_LINE="$(grep -n '^rewrite_installed_cellular_polkit_rule$' "$INSTALL_SH" | head -1 | cut -d: -f1)"
APT_LINE="$(grep -nE '^[[:space:]]*\| xargs sudo apt install -y' "$INSTALL_SH" | head -1 | cut -d: -f1)"
[[ -n "$APT_LINE" && "$CALL_LINE" -lt "$APT_LINE" ]] \
    || fail "the polkit migration runs at line $CALL_LINE, after Step 1's apt (line $APT_LINE)"

# shellcheck source=/dev/null
source "$FUNC_FILE"
BLACKBOX_ROOT="$REPO_ROOT"
REAL_USER="bbx"
INSTALL_SUDO=""

# 5a. an old broad rule is rewritten in place
M1="$WORK/m1/rules.d"; mkdir -p "$M1"
cat > "$M1/$RULE_NAME" <<'OLD_RULE'
polkit.addRule(function(action, subject) {
    if (subject.user !== "bbx") return undefined;
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0) {
        return polkit.Result.YES;
    }
    if (action.id === "org.freedesktop.systemd1.manage-units") {
        return polkit.Result.YES;
    }
    return undefined;
});
OLD_RULE
PATH="$WORK/bin:$PATH" POLKIT_RULES_DIR="$M1" rewrite_installed_cellular_polkit_rule >"$WORK/m1.log" 2>&1 \
    || fail "migration returned non-zero: $(cat "$WORK/m1.log")"
assert_tight_rule "$M1/$RULE_NAME" bbx

# 5b. no rule installed → nothing created (the rule stays opt-in)
M2="$WORK/m2/rules.d"; mkdir -p "$M2"
PATH="$WORK/bin:$PATH" POLKIT_RULES_DIR="$M2" rewrite_installed_cellular_polkit_rule >"$WORK/m2.log" 2>&1 \
    || fail "migration returned non-zero on a box without the rule"
[[ -e "$M2/$RULE_NAME" ]] && fail "migration created the opt-in rule on a box that never installed it"

# 5c. a rewrite that fails must not leave the broad rule behind
M3="$WORK/m3/rules.d"; mkdir -p "$M3"
printf 'if (action.id === "org.freedesktop.systemd1.manage-units") {}\n' > "$M3/$RULE_NAME"
REAL_USER='bad"user' PATH="$WORK/bin:$PATH" POLKIT_RULES_DIR="$M3" \
    rewrite_installed_cellular_polkit_rule >"$WORK/m3.log" 2>&1
[[ -e "$M3/$RULE_NAME" ]] && grep -q 'systemd1' "$M3/$RULE_NAME" \
    && fail "a failed rewrite left the broad manage-units rule installed"

# ── 6. production pins (source-level — a sandbox cannot exercise them) ────────
grep -qE '^RULES_DIR="\$\{POLKIT_RULES_DIR:-/etc/polkit-1/rules\.d\}"' "$SCRIPT" \
    || fail "setup-cellular-polkit.sh no longer defaults to /etc/polkit-1/rules.d"
grep -qE '^set -euo pipefail' "$SCRIPT" || fail "setup-cellular-polkit.sh is not set -euo pipefail"

echo "ALL TESTS PASSED"
exit 0
