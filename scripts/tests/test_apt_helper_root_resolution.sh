#!/usr/bin/env bash
# blackbox-apt-install trusts ONLY the root-owned /etc allowlist.
#
# install.sh-grant removal, 2026-07-27: the helper used to resolve its allowlist
# through an env → /etc/blackbox/root pointer → templated-root LADDER that ended
# at the in-tree $BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt — a file
# inside blackbox.service's ReadWritePaths, i.e. service-writable, i.e. the
# standing RCE gap. The helper now reads ONLY /etc/blackbox/system-packages.txt.
# (BLACKBOX_SYSTEM_PACKAGES overrides that path for THIS harness; sudo's
# env_reset strips the override on a real invocation through the NOPASSWD grant,
# so production can never be pointed elsewhere.)
#
# This test proves the properties that make the /etc-only source a real boundary:
#   1. the hardcoded source is /etc/blackbox/system-packages.txt (no repo path);
#   2. a package present in the REPO allowlist but ABSENT from /etc is REJECTED
#      — the closed door (the repo allowlist is never consulted);
#   3. an /etc-listed package passes the membership gate (the source is honoured);
#   4. an absent /etc copy FAILS CLOSED (exit 4) naming the operator remedy.
#
# The helper under test is the INSTALLED (templated) artifact, so this installs
# it into a sandbox first via install.sh's install_update_helpers() — same sed
# extraction trick as scripts/tests/test_install_update_helpers.sh.
#
# SAFETY: every probe package that reaches the apt line is a fabricated name apt
# will simply fail to find, and the assertions key on the pre-apt "(allowlisted)"
# echo, never on apt success — so this test needs no root and installs nothing.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
HELPER_TEMPLATE="$REPO_ROOT/installer/templates/blackbox-apt-install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
sed -n '/^install_update_helpers()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
grep -q 'install_update_helpers()' "$FUNC_FILE" \
    || fail "could not extract install_update_helpers() from install.sh"

# NOT exported (I1): plain shell vars reach the SOURCED function below, and an
# export would leak into a later `bash install.sh` in the same shell. Every
# helper subprocess invocation in this file passes its env per-invocation.
BLACKBOX_ROOT="$REPO_ROOT"
SBIN_DIR="$WORK/usr/local/sbin"
ETC_BLACKBOX_DIR="$WORK/etc/blackbox"
INSTALL_SUDO=""
INSTALL_OWNER="$(id -un)"
INSTALL_GROUP="$(id -gn)"
mkdir -p "$SBIN_DIR"

# shellcheck source=/dev/null
source "$FUNC_FILE"
install_update_helpers >/dev/null 2>&1 || fail "sandbox helper install failed"
unset BLACKBOX_ROOT   # the helper must not need it any more

HELPER="$SBIN_DIR/blackbox-apt-install"
[[ -x "$HELPER" ]] || fail "blackbox-apt-install was not installed"

# ── 1. source-level: the hardcoded source is /etc, with no repo fallback ─────
grep -qF 'ALLOWLIST_FILE="${BLACKBOX_SYSTEM_PACKAGES:-/etc/blackbox/system-packages.txt}"' \
    "$HELPER_TEMPLATE" \
    || fail "helper no longer hardcodes /etc/blackbox/system-packages.txt as its sole source"
# The former ladder tokens must be GONE — no pointer, no env-derived repo root.
grep -q 'BLACKBOX_ROOT_POINTER' "$HELPER_TEMPLATE" \
    && fail "the /etc/blackbox/root pointer is still resolved — the ladder was not removed"
grep -q 'BLACKBOX_ROOT_PLACEHOLDER' "$HELPER_TEMPLATE" \
    && fail "a templated install-root default survives — the helper still resolves a repo path"

# ── 2. closed door: a REPO-allowlisted package absent from /etc is REJECTED ───
# The first real package in the repo allowlist would be accepted by any helper
# that still consulted the repo. Pointed at an /etc copy WITHOUT it, the helper
# must reject it (exit 5) — proving the repo allowlist is never read.
REPO_PKG="$(grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' \
              "$REPO_ROOT/Scripts/onboarding/system-packages.txt" | awk '{print $1}' | head -1)"
[[ -n "$REPO_PKG" ]] || fail "could not read a package from the repo allowlist"

ETC_WITHOUT="$WORK/etc-without.txt"
printf 'someunrelatedpkg  # MUST_HAVE # not the repo pkg\n' > "$ETC_WITHOUT"
OUT="$(BLACKBOX_SYSTEM_PACKAGES="$ETC_WITHOUT" bash "$HELPER" "$REPO_PKG" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] \
    || fail "closed door BREACHED: repo-allowlisted '$REPO_PKG' not in /etc was accepted (rc=$RC): $OUT"

# ── 3. an /etc-listed package passes the membership gate ─────────────────────
# Prove the /etc source is honoured POSITIVELY: a fabricated but regex-valid
# name listed in the /etc copy must reach the pre-apt "(allowlisted)" echo. We
# key on that echo (printed right before /usr/bin/apt-get) so the test never
# depends on apt succeeding — apt will simply fail to find the fake package.
ETC_WITH="$WORK/etc-with.txt"
FAKE_PKG="zzz-etc-listed-probe"
printf '%s  # MUST_HAVE # fabricated probe\n' "$FAKE_PKG" > "$ETC_WITH"
OUT="$(BLACKBOX_SYSTEM_PACKAGES="$ETC_WITH" bash "$HELPER" "$FAKE_PKG" 2>&1)"; RC=$?
grep -qF "Installing $FAKE_PKG (allowlisted)" <<<"$OUT" \
    || fail "an /etc-listed package did not pass the membership gate (rc=$RC): $OUT"

# ── 4. absent /etc copy fails closed, naming the operator remedy ─────────────
NOPE="$WORK/no-such-allowlist.txt"
OUT="$(BLACKBOX_SYSTEM_PACKAGES="$NOPE" bash "$HELPER" "$REPO_PKG" 2>&1)"; RC=$?
[[ "$RC" -eq 4 ]] || fail "absent /etc allowlist did not fail closed (expected exit 4, got $RC): $OUT"
grep -qF "$NOPE" <<<"$OUT" \
    || fail "exit-4 message does not name the allowlist source it tried to read: $OUT"
grep -q 'install.sh' <<<"$OUT" \
    || fail "exit-4 message names no remedy (the operator step): $OUT"

# ── 5. perimeter: the regex + argument gates still reject ─────────────────────
OUT="$(BLACKBOX_SYSTEM_PACKAGES="$ETC_WITH" bash "$HELPER" 'novnc; rm -rf /' 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]] || fail "package-name regex gate no longer rejects metacharacters (rc=$RC)"

OUT="$(BLACKBOX_SYSTEM_PACKAGES="$ETC_WITH" bash "$HELPER" '../../etc/passwd' 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]] || fail "package-name regex gate no longer rejects path traversal (rc=$RC)"

OUT="$(BLACKBOX_SYSTEM_PACKAGES="$ETC_WITH" bash "$HELPER" 2>&1)"; RC=$?
[[ "$RC" -eq 2 ]] || fail "missing-argument gate changed (rc=$RC)"

echo "ALL TESTS PASSED"
exit 0
