#!/usr/bin/env bash
# Test for install.sh's install_update_helpers() function.
#
# Guards two properties of install_update_helpers():
#   1. It installs the bounded dispatch helpers templated (not verbatim) and
#      root-owned under /usr/local/sbin.
#   2. install.sh-grant removal, 2026-07-27: it writes the ROOT-OWNED apt
#      allowlist copy /etc/blackbox/system-packages.txt — now blackbox-apt-
#      install's SOLE trusted source (it replaced the /etc/blackbox/root
#      pointer). install.sh is the ONLY writer of that /etc copy; the perimeter
#      test forbids the service ever writing /etc/blackbox.
# (Historic context: the helpers were once installed VERBATIM, so a moved repo
# stranded blackbox-apt-install at a nonexistent allowlist path — the reason
# templating + a fixed /etc source exist.)
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
ETC_ALLOWLIST="$ETC_BLACKBOX_DIR/system-packages.txt"

# ── Assert: both helpers landed ───────────────────────────────────────────────
[[ -f "$HELPER" ]] || fail "blackbox-apt-install was not installed"
[[ -f "$SBIN_DIR/blackbox-write-systemd" ]] || fail "blackbox-write-systemd was not installed"

# ── Assert: the installed artifact is TEMPLATED, not a verbatim copy ──────────
# No helper carries a BLACKBOX_ROOT_PLACEHOLDER today (apt-install now reads the
# fixed /etc allowlist), but the substitution machinery stays for future helpers
# — a surviving PLACEHOLDER would mean the sed step silently broke.
if grep -q 'PLACEHOLDER' "$HELPER"; then
    fail "PLACEHOLDER survived into the installed helper — it was copied verbatim"
fi

# ── Assert: root-owned apt allowlist written from the repo copy ───────────────
# install.sh-grant removal, 2026-07-27: this /etc copy is blackbox-apt-install's
# ONLY trusted source, and install.sh is its ONLY writer.
[[ -f "$ETC_ALLOWLIST" ]] || fail "root-owned /etc allowlist was not written"
diff -q "$REPO_ROOT/Scripts/onboarding/system-packages.txt" "$ETC_ALLOWLIST" >/dev/null \
    || fail "/etc allowlist is not a byte-for-byte copy of the repo allowlist"

# ── Assert: modes unchanged (0755 helpers / 0644 allowlist are perimeter) ─────
[[ "$(stat -c '%a' "$HELPER")" == "755" ]] \
    || fail "helper mode is $(stat -c '%a' "$HELPER"), expected 755"
[[ "$(stat -c '%a' "$ETC_ALLOWLIST")" == "644" ]] \
    || fail "/etc allowlist mode is $(stat -c '%a' "$ETC_ALLOWLIST"), expected 644"

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
    || fail "/etc allowlist install mode is no longer 0644"

# ── Run 2 (idempotency) ───────────────────────────────────────────────────────
install_update_helpers >/dev/null 2>&1 || fail "function returned non-zero on second run"
diff -q "$REPO_ROOT/Scripts/onboarding/system-packages.txt" "$ETC_ALLOWLIST" >/dev/null \
    || fail "/etc allowlist corrupted on re-run"
grep -q 'PLACEHOLDER' "$HELPER" && fail "PLACEHOLDER reappeared on re-run"

# ── Run 3: an install root full of shell-hostile metacharacters ───────────────
# install.sh-grant removal, 2026-07-27: the apt helper no longer bakes the root
# (so the sed-`&` escaping incident is designed out for it), but install.sh must
# still COPY the /etc allowlist from a repo path riddled with spaces, `&`, `.` —
# `install` takes the source as an argv, so this exercises that the copy handles
# it. Space + `.` are also here because the dev box lives under a path with a `.`
# in a directory name.
WEIRD_ROOT="$WORK/r&d lab v2./repo"
mkdir -p "$WEIRD_ROOT/installer" "$WEIRD_ROOT/Scripts/onboarding"
cp -r "$REPO_ROOT/installer/templates" "$WEIRD_ROOT/installer/templates"
printf 'novnc  # SHOULD_HAVE # CU live-view browser client\n' \
    > "$WEIRD_ROOT/Scripts/onboarding/system-packages.txt"

WEIRD_SBIN="$WORK/weird/sbin"; mkdir -p "$WEIRD_SBIN"
WEIRD_ETC="$WORK/weird/etc/blackbox"
BLACKBOX_ROOT="$WEIRD_ROOT" SBIN_DIR="$WEIRD_SBIN" \
    ETC_BLACKBOX_DIR="$WEIRD_ETC" \
    install_update_helpers >/dev/null 2>&1 \
    || fail "function returned non-zero installing from a metacharacter root"

WEIRD_HELPER="$WEIRD_SBIN/blackbox-apt-install"
grep -q 'PLACEHOLDER' "$WEIRD_HELPER" \
    && fail "an unescaped sed metacharacter left PLACEHOLDER in the installed helper"
diff -q "$WEIRD_ROOT/Scripts/onboarding/system-packages.txt" "$WEIRD_ETC/system-packages.txt" >/dev/null \
    || fail "/etc allowlist copy did not round-trip a metacharacter root"

# End-to-end: pointed at that /etc allowlist, the installed helper reads it and
# refuses an unlisted package (exit 5). Proves the helper's sole source works
# through the copy install.sh just wrote. exit 4 would be the failure mode.
OUT="$(env -u BLACKBOX_ROOT BLACKBOX_SYSTEM_PACKAGES="$WEIRD_ETC/system-packages.txt" \
       bash "$WEIRD_HELPER" zzz-probe-not-in-any-allowlist 2>&1)"; RC=$?
[[ "$RC" -eq 5 ]] \
    || fail "helper cannot read the /etc allowlist install.sh wrote (rc=$RC): $OUT"

echo "ALL TESTS PASSED"
exit 0
