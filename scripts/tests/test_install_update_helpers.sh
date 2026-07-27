#!/usr/bin/env bash
# Test for install.sh's install_update_helpers() function.
#
# Guards the root cause of the stale-helper incident (customer, 2026-07-27):
# the update-pipeline dispatch helpers used to be installed VERBATIM, so
# blackbox-apt-install shipped with a hardcoded guess at the install root and
# a box installed anywhere else pointed its allowlist lookup at a directory
# that did not exist. Because the installed copy is root-owned under
# /usr/local/sbin, `git reset --hard` could never repair it.
#
# install.sh executes its install steps at top level (no main() guard), so
# sourcing it directly would run the whole installer (and require root/sudo).
# Instead we EXTRACT just the function definition via sed into a temp file and
# source that, then run it against a mktemp sandbox with path overrides — never
# touching the real /usr/local/sbin or /etc. INSTALL_SUDO= runs it unprivileged.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

# ── Extract the function into a sourceable temp file ──────────────────────────
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
sed -n '/^install_update_helpers()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
if ! grep -q 'install_update_helpers()' "$FUNC_FILE"; then
    fail "could not extract install_update_helpers() from install.sh"
fi

# ── Sandbox: redirect every system path into the temp dir ─────────────────────
# NOT exported (I1): these override install destinations/owner/escalation, and
# an export would leak into any later `bash install.sh` in the same shell. The
# function under test is SOURCED, so plain shell vars reach it fine; the
# subprocess helper invocations below pass what they need per-invocation.
BLACKBOX_ROOT="$REPO_ROOT"
SBIN_DIR="$WORK/usr/local/sbin"
ETC_BLACKBOX_DIR="$WORK/etc/blackbox"
INSTALL_SUDO=""                 # run direct, no privilege escalation
INSTALL_OWNER="$(id -un)"       # can't chown to root unprivileged
INSTALL_GROUP="$(id -gn)"
mkdir -p "$SBIN_DIR"

# shellcheck source=/dev/null
source "$FUNC_FILE"

install_update_helpers >/dev/null 2>&1 || fail "function returned non-zero on first run"

HELPER="$SBIN_DIR/blackbox-apt-install"
POINTER="$ETC_BLACKBOX_DIR/root"

# ── Assert: both helpers landed ───────────────────────────────────────────────
[[ -f "$HELPER" ]] || fail "blackbox-apt-install was not installed"
[[ -f "$SBIN_DIR/blackbox-write-systemd" ]] || fail "blackbox-write-systemd was not installed"

# ── Assert: the installed artifact is TEMPLATED, not a verbatim copy ──────────
if grep -q 'PLACEHOLDER' "$HELPER"; then
    fail "PLACEHOLDER survived into the installed helper — it was copied verbatim"
fi

# ── Assert: the baked default IS this install root, not a canonical guess ─────
grep -qF "BLACKBOX_ROOT:-${REPO_ROOT}}" "$HELPER" \
    || fail "installed helper's default root is not the real install root ($REPO_ROOT)"

# The pre-rename canonical path is what the customer's stale helper carried.
# Skip the check on a box that genuinely lives there (nothing to distinguish).
if [[ "$REPO_ROOT" != /home/bbx/Desktop/* ]]; then
    grep -q '/home/bbx/Desktop' "$HELPER" \
        && fail "installed helper still carries a hardcoded canonical install path"
fi

# ── Assert: root pointer written with the real root ───────────────────────────
[[ -f "$POINTER" ]] || fail "install-root pointer was not written"
[[ "$(head -n 1 "$POINTER")" == "$REPO_ROOT" ]] \
    || fail "pointer contents ($(head -n 1 "$POINTER")) != install root ($REPO_ROOT)"

# ── Assert: modes unchanged (0755 helpers / 0644 pointer are perimeter) ───────
[[ "$(stat -c '%a' "$HELPER")" == "755" ]] \
    || fail "helper mode is $(stat -c '%a' "$HELPER"), expected 755"
[[ "$(stat -c '%a' "$POINTER")" == "644" ]] \
    || fail "pointer mode is $(stat -c '%a' "$POINTER"), expected 644"

# ── Assert: ownership/escalation are PINNED for production destinations ───────
# The sandbox above can only run unprivileged because it redirected BOTH
# destinations into mktemp. install.sh is designed to run unprivileged (Step 0),
# so the caller's env reaches the function — and an exported INSTALL_OWNER
# aimed at the REAL /usr/local/sbin would hand the service user write access to
# a helper that root executes through the NOPASSWD grant. That branch cannot be
# exercised from a sandbox, so assert it at the source level instead.
grep -q 'if \[\[ "\$SBIN_DIR" == "/usr/local/sbin" || "\$ETC_BLACKBOX_DIR" == "/etc/blackbox" \]\]' "$INSTALL_SH" \
    || fail "production destinations are no longer pinned — INSTALL_OWNER/INSTALL_SUDO could aim at the real perimeter"
grep -q 'OWNER=root; GROUP=root; SUDO=sudo' "$INSTALL_SH" \
    || fail "the production branch no longer forces root:root + sudo"
grep -qF 'INSTALL_OWNER:-root' "$INSTALL_SH" || fail "sandbox helper owner default is no longer root"
grep -qF 'INSTALL_GROUP:-root' "$INSTALL_SH" || fail "sandbox helper group default is no longer root"
grep -qF 'install -m 0755 -o "$OWNER" -g "$GROUP"' "$INSTALL_SH" \
    || fail "helper install mode is no longer 0755"
grep -qF 'install -m 0644 -o "$OWNER" -g "$GROUP"' "$INSTALL_SH" \
    || fail "pointer install mode is no longer 0644"

# ── Run 2 (idempotency) ───────────────────────────────────────────────────────
install_update_helpers >/dev/null 2>&1 || fail "function returned non-zero on second run"
[[ "$(head -n 1 "$POINTER")" == "$REPO_ROOT" ]] || fail "pointer corrupted on re-run"
grep -q 'PLACEHOLDER' "$HELPER" && fail "PLACEHOLDER reappeared on re-run"

# ── Run 3: an install root full of sed replacement metacharacters ─────────────
# MEASURED before the escaping fix: a root of /home/r&d/repo baked
# /home/rBLACKBOX_ROOT_PLACEHOLDERd/repo into the helper (in sed a bare `&` is
# "the whole match"), which is the incident reintroduced silently — and the
# pointer could not rescue it either. Space + `.` are here because the dev box
# itself lives under a path with a `.` in a directory name.
WEIRD_ROOT="$WORK/r&d lab v2./repo"
mkdir -p "$WEIRD_ROOT/installer" "$WEIRD_ROOT/Scripts/onboarding"
cp -r "$REPO_ROOT/installer/templates" "$WEIRD_ROOT/installer/templates"
printf 'novnc  # SHOULD_HAVE # CU live-view browser client\n' \
    > "$WEIRD_ROOT/Scripts/onboarding/system-packages.txt"

WEIRD_SBIN="$WORK/weird/sbin"; mkdir -p "$WEIRD_SBIN"
BLACKBOX_ROOT="$WEIRD_ROOT" SBIN_DIR="$WEIRD_SBIN" \
    ETC_BLACKBOX_DIR="$WORK/weird/etc/blackbox" \
    install_update_helpers >/dev/null 2>&1 \
    || fail "function returned non-zero installing from a metacharacter root"

WEIRD_HELPER="$WEIRD_SBIN/blackbox-apt-install"
grep -qF "BLACKBOX_ROOT:-${WEIRD_ROOT}}" "$WEIRD_HELPER" \
    || fail "baked default is not byte-for-byte the install root: $(grep 'BLACKBOX_ROOT:-' "$WEIRD_HELPER")"
grep -q 'PLACEHOLDER' "$WEIRD_HELPER" \
    && fail "an unescaped sed metacharacter left PLACEHOLDER in the installed helper"
[[ "$(head -n 1 "$WORK/weird/etc/blackbox/root")" == "$WEIRD_ROOT" ]] \
    || fail "pointer does not round-trip a metacharacter root"

# End-to-end: with no env and no pointer, the baked default alone must reach the
# allowlist (exit 5 = read it and refused an unlisted package; exit 4 = the bug).
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$WORK/no-such-pointer" \
       bash "$WEIRD_HELPER" zzz-probe-not-in-any-allowlist 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] \
    || fail "helper installed from a metacharacter root cannot read its allowlist (rc=$RC): $OUT"

echo "ALL TESTS PASSED"
exit 0
