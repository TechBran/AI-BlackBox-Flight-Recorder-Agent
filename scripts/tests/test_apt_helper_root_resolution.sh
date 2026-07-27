#!/usr/bin/env bash
# Test for blackbox-apt-install's install-root resolution ladder.
#
# Customer, 2026-07-27: the helper resolved its allowlist from ONE hardcoded
# constant, so a stale installed copy sent every apt phase at a directory that
# did not exist (exit 4). It now resolves env → root pointer → templated
# default, so neither a moved repo nor a rename can strand a box.
#
# The helper under test is the INSTALLED (templated) artifact, so this installs
# it into a sandbox first via install.sh's install_update_helpers() — same sed
# extraction trick as scripts/tests/test_install_asterisk_block.sh.
#
# SAFETY: every probe package here is deliberately absent from the allowlist,
# so the helper always exits at the membership check (5) or the readability
# check (4). apt-get is never reached.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

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
unset BLACKBOX_ROOT   # each case below decides for itself

HELPER="$SBIN_DIR/blackbox-apt-install"
PROBE="zzz-probe-not-in-any-allowlist"
NO_POINTER="$WORK/etc/blackbox/absent"

ALLOWLIST_BODY='novnc  # SHOULD_HAVE # CU live-view browser client'

seed_root() {  # $1 = root dir to fabricate
    mkdir -p "$1/Scripts/onboarding"
    printf '%s\n' "$ALLOWLIST_BODY" > "$1/Scripts/onboarding/system-packages.txt"
}

# ── 1. $BLACKBOX_ROOT wins over everything ───────────────────────────────────
ENV_ROOT="$WORK/envroot"; seed_root "$ENV_ROOT"
POINTER_ROOT="$WORK/pointroot"; seed_root "$POINTER_ROOT"
POINTER_FILE="$WORK/pointer"; printf '%s\n' "$POINTER_ROOT" > "$POINTER_FILE"

OUT="$(BLACKBOX_ROOT="$ENV_ROOT" BLACKBOX_ROOT_POINTER="$POINTER_FILE" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "env-root case: expected exit 5 (not allowlisted), got $RC: $OUT"
grep -qF "$ENV_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "env BLACKBOX_ROOT did not win: $OUT"

# ── 2. pointer file used when the env var is absent (the production path) ────
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$POINTER_FILE" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "pointer case: expected exit 5, got $RC: $OUT"
grep -qF "$POINTER_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "root pointer was not honoured: $OUT"

# ── 3. templated default when neither env nor pointer resolves ───────────────
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$NO_POINTER" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "default case: expected exit 5, got $RC: $OUT"
grep -qF "$REPO_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "templated default is not the real install root: $OUT"

# ── 3b. a pointer whose path contains spaces/parens is HONOURED ──────────────
# The first cut validated the pointer with a character-class regex and MEASURED
# a root of ".../black box (2)/repo" being rejected on every single invocation —
# silently voiding the pointer for precisely the moved-repo case it exists for.
# Validation is structural now (absolute + the allowlist is readable there).
SPACED_ROOT="$WORK/black box (2)/repo"; seed_root "$SPACED_ROOT"
SPACED_POINTER="$WORK/spaced-pointer"; printf '%s\n' "$SPACED_ROOT" > "$SPACED_POINTER"
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$SPACED_POINTER" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "spaced-root pointer case: expected exit 5, got $RC: $OUT"
grep -qF "$SPACED_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "a pointer with spaces/parens in the path was rejected: $OUT"
grep -q 'ignoring unusable root pointer' <<<"$OUT" \
    && fail "a perfectly valid pointer was warned about: $OUT"

# ── 4. unusable pointer is ignored, NOT obeyed ───────────────────────────────
# Only root can write the pointer, but a truncated or garbage value must fail
# closed to the templated default rather than sending apt at a surprise tree.
BAD_POINTER="$WORK/bad-pointer"
printf 'relative/path; rm -rf /\n' > "$BAD_POINTER"
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$BAD_POINTER" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "malformed-pointer case: expected exit 5, got $RC: $OUT"
grep -q 'ignoring unusable root pointer' <<<"$OUT" \
    || fail "malformed pointer was accepted silently: $OUT"
grep -qF "$REPO_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "malformed pointer did not fall back to the templated default: $OUT"

# 4b. absolute, but not a BlackBox tree — same treatment (it cannot serve an
# allowlist, so obeying it would just convert a working box into an exit-4 box).
EMPTY_DIR="$WORK/absolute-but-empty"; mkdir -p "$EMPTY_DIR"
STRAY_POINTER="$WORK/stray-pointer"; printf '%s\n' "$EMPTY_DIR" > "$STRAY_POINTER"
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$STRAY_POINTER" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "stray-pointer case: expected exit 5, got $RC: $OUT"
grep -qF "$REPO_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "a pointer at a non-BlackBox directory was obeyed: $OUT"

# 4c. a pointer whose path contains a `..` component is REJECTED, even when it
# resolves to a real BlackBox tree. The structural check rejects `..` outright
# rather than let a pointer traverse out of the intended tree (I2). It must fall
# through to the templated default with the same warning as any unusable pointer
# (rc=5 here because this sandbox's templated default, $REPO_ROOT, is itself a
# readable BlackBox tree — on the customer's stale box the default is unreadable
# and this same fall-through surfaces as the exit-4 they saw).
DOTDOT_BASE="$WORK/dotdot"; seed_root "$DOTDOT_BASE/repo"
DOTDOT_POINTER="$WORK/dotdot-pointer"
printf '%s\n' "$DOTDOT_BASE/repo/../repo" > "$DOTDOT_POINTER"
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_ROOT_POINTER="$DOTDOT_POINTER" \
       bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] || fail "dotdot-pointer case: expected exit 5 (rejected, fall-through to default), got $RC: $OUT"
grep -q 'ignoring unusable root pointer' <<<"$OUT" \
    || fail "a pointer with a .. component was accepted instead of rejected: $OUT"
grep -qF "$REPO_ROOT/Scripts/onboarding/system-packages.txt" <<<"$OUT" \
    || fail "a rejected .. pointer did not fall through to the templated default: $OUT"

# ── 5. unreadable allowlist still exits 4, now with an actionable remedy ─────
DEAD_ROOT="$WORK/moved-away"; mkdir -p "$DEAD_ROOT"
OUT="$(BLACKBOX_ROOT="$DEAD_ROOT" bash "$HELPER" "$PROBE" 2>&1)"; RC=$?
[[ "$RC" -eq 4 ]] || fail "unreadable-allowlist case: expected exit 4, got $RC: $OUT"
grep -q 'Scripts/install.sh' <<<"$OUT" \
    || fail "exit-4 message names no remedy — this is the message the customer saw: $OUT"

# ── 6. perimeter: the regex + allowlist gates must still reject ──────────────
OUT="$(BLACKBOX_ROOT="$ENV_ROOT" bash "$HELPER" 'novnc; rm -rf /' 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]] || fail "package-name regex gate no longer rejects metacharacters (rc=$RC)"

OUT="$(BLACKBOX_ROOT="$ENV_ROOT" bash "$HELPER" '../../etc/passwd' 2>&1)"; RC=$?
[[ "$RC" -eq 3 ]] || fail "package-name regex gate no longer rejects path traversal (rc=$RC)"

OUT="$(BLACKBOX_ROOT="$ENV_ROOT" bash "$HELPER" 2>&1)"; RC=$?
[[ "$RC" -eq 2 ]] || fail "missing-argument gate changed (rc=$RC)"

echo "ALL TESTS PASSED"
exit 0
