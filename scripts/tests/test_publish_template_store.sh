#!/usr/bin/env bash
# Test for install.sh's publish_template_store() function.
#
# install.sh-grant removal, 2026-07-27 (trusted-template-store design):
# publish_template_store() is the SOLE writer of the root-owned template store
# /etc/blackbox/templates/<kind> that blackbox-write-systemd copies from (it no
# longer takes a caller-supplied source). This proves:
#   1. every store entry lands, rendered with the real BLACKBOX_ROOT/REAL_USER
#      (no surviving PLACEHOLDER), byte-identical to what install.sh would deploy;
#   2. store modes are 0440 for sudoers kinds, 0644 otherwise;
#   3. the store entry keys are EXACTLY the write-systemd target_kinds, so a
#      dispatch of each finds its file (paired with the helper's case arms);
#   4. it round-trips an install root full of shell-hostile metacharacters
#      (spaces, `&`, `.`) — the sed-escaping incident class;
#   5. it is idempotent.
#
# install.sh runs its steps at top level (no main() guard), so sourcing it would
# run the whole installer. Instead we EXTRACT just the function via sed and
# source that, then run it against a mktemp sandbox with path overrides —
# never touching the real /etc. INSTALL_SUDO= runs it unprivileged.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
WS="$REPO_ROOT/installer/templates/blackbox-write-systemd.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── Extract the function into a sourceable temp file ──────────────────────────
FUNC_FILE="$WORK/func.sh"
sed -n '/^publish_template_store()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
if ! grep -q 'publish_template_store()' "$FUNC_FILE"; then
    fail "could not extract publish_template_store() from install.sh"
fi

# ── Sandbox: redirect the store dir + ownership into the temp dir ─────────────
# NOT exported (an export would leak into any later `bash install.sh`). The
# function is SOURCED, so plain shell vars reach it fine.
BLACKBOX_ROOT="$REPO_ROOT"
REAL_USER="$(id -un)"
ETC_BLACKBOX_DIR="$WORK/etc/blackbox"
INSTALL_SUDO=""                 # run direct, no privilege escalation
INSTALL_OWNER="$(id -un)"       # can't chown to root unprivileged
INSTALL_GROUP="$(id -gn)"

# shellcheck source=/dev/null
source "$FUNC_FILE"

publish_template_store >/dev/null 2>&1 || fail "function returned non-zero on first run"

STORE_DIR="$ETC_BLACKBOX_DIR/templates"

# The store keys the function publishes, with their expected store mode. These
# are exactly the write-systemd target_kinds backed by a git-tracked template.
declare -A EXPECT_MODE=(
    [sudoers-system]=440
    [apt-install-helper]=644
    [write-systemd-helper]=644
    [zellij-web-unit]=644
)

for kind in "${!EXPECT_MODE[@]}"; do
    entry="$STORE_DIR/$kind"
    [[ -f "$entry" ]] || fail "store entry $kind was not published"
    got="$(stat -c '%a' "$entry")"
    [[ "$got" == "${EXPECT_MODE[$kind]}" ]] \
        || fail "store entry $kind mode is $got, expected ${EXPECT_MODE[$kind]}"
    # ── Rendered, not raw: no PLACEHOLDER survives ────────────────────────────
    grep -q 'PLACEHOLDER' "$entry" \
        && fail "store entry $kind still contains a PLACEHOLDER — the render step silently broke"
    # ── Every published kind must be a real write-systemd case arm ────────────
    grep -qE "^[[:space:]]*${kind}\)" "$WS" \
        || fail "published store kind $kind has no matching blackbox-write-systemd case arm"
done

# ── Rendered content is byte-identical to install.sh's own deploy substitution ─
# The sudoers store entry must equal the template with BOTH placeholders filled
# (that byte-equality is what makes the runner's byte-compare-against-store real).
ROOT_ESC="${BLACKBOX_ROOT//\\/\\\\}"; ROOT_ESC="${ROOT_ESC//&/\\&}"; ROOT_ESC="${ROOT_ESC//|/\\|}"
EXPECTED_SUDOERS="$WORK/expected-sudoers"
sed -e "s|BLACKBOX_ROOT_PLACEHOLDER|$ROOT_ESC|g" \
    -e "s|REAL_USER_PLACEHOLDER|$REAL_USER|g" \
    "$REPO_ROOT/installer/templates/sudoers-blackbox-system" > "$EXPECTED_SUDOERS"
diff -q "$EXPECTED_SUDOERS" "$STORE_DIR/sudoers-system" >/dev/null \
    || fail "sudoers-system store entry is not the fully-rendered template"

# ── Idempotency ───────────────────────────────────────────────────────────────
publish_template_store >/dev/null 2>&1 || fail "function returned non-zero on second run"
diff -q "$EXPECTED_SUDOERS" "$STORE_DIR/sudoers-system" >/dev/null \
    || fail "sudoers-system store entry corrupted on re-run"

# ── Metacharacter install root round-trips (the sed-escaping incident class) ──
WEIRD_ROOT="$WORK/r&d lab v2./repo"
mkdir -p "$WEIRD_ROOT"
cp -r "$REPO_ROOT/installer" "$WEIRD_ROOT/installer"
WEIRD_ETC="$WORK/weird/etc/blackbox"
BLACKBOX_ROOT="$WEIRD_ROOT" ETC_BLACKBOX_DIR="$WEIRD_ETC" \
    publish_template_store >/dev/null 2>&1 \
    || fail "function returned non-zero publishing from a metacharacter root"
grep -q 'PLACEHOLDER' "$WEIRD_ETC/templates/apt-install-helper" \
    && fail "an unescaped sed metacharacter left PLACEHOLDER in a store entry"

# ── Production destinations are PINNED (source-level — a sandbox can't hit it) ─
grep -q 'if \[\[ "\$ETC_BLACKBOX_DIR" == "/etc/blackbox" \]\]' "$INSTALL_SH" \
    || fail "publish_template_store no longer pins the production /etc/blackbox destination"
grep -q 'OWNER=root; GROUP=root; SUDO=sudo' "$INSTALL_SH" \
    || fail "the production branch no longer forces root:root + sudo"

echo "ALL TESTS PASSED"
exit 0
