#!/usr/bin/env bash
# Test for install.sh Step 5, the BlackBox Setup (Tauri .deb) build.
#
# 2026-10-09 audit O22-03 (merged into O22-01, rank 40): build_tauri_setup ran
# rustup, npm and cargo as ROOT inside the user-owned checkout on every
# install.sh and update.sh run — root-owned target/ and node_modules/ the next
# user build cannot write, a toolchain in root's home, and a multi-minute
# rebuild of an unchanged app on every update.
#
# Proves, against the real Step 5 functions extracted from install.sh and run
# in a mktemp sandbox (PATH stubs for sudo, cargo, npm, apt and dpkg; nothing
# is built or installed):
#   1. the toolchain and the build run as $REAL_USER with HOME=$REAL_HOME;
#   2. an installed app at the source version is not rebuilt;
#   3. a missing app, a version change, or BLACKBOX_BUILD_TAURI=1 builds;
#      BLACKBOX_BUILD_TAURI=0 never does;
#   4. a package renamed by the productName change replaces the old one
#      (both ship /usr/bin/blackbox-setup, which dpkg refuses to overwrite);
#   5. build artifacts owned by someone else (an old root build) are reported,
#      not built over — at any depth: an old root build leaves root-owned
#      release/deps/ and bundle/deb/<pkg>/ entries under a target/ that is
#      itself user-owned; a failed rebuild keeps the installed app, and only a
#      box with no setup app at all fails the install.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
# shellcheck source=scripts/tests/lib/installer_stubs.sh
source "$REPO_ROOT/scripts/tests/lib/installer_stubs.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
stub_kit_init "$WORK"

extract() {
    sed -n "/^$1()[[:space:]]*{/,/^}/p" "$INSTALL_SH" >> "$FUNC_FILE"
    grep -q "^$1()" "$FUNC_FILE" || fail "could not extract $1() from install.sh"
}

ROOT="$WORK/root"
mkdir -p "$ROOT/installer/src-tauri"
cp "$REPO_ROOT/installer/src-tauri/tauri.conf.json" "$ROOT/installer/src-tauri/"
SRC_VER="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/installer/src-tauri/tauri.conf.json")"
DEB_DIR="$ROOT/installer/src-tauri/target/release/bundle/deb"

# cargo stub: records who ran it and with which HOME; `tauri build` drops a .deb.
cat > "$STUB_BIN/cargo" <<EOF
#!/usr/bin/env bash
echo "cargo[user=\${STUB_SUDO_USER:-} home=\$HOME] \$*" >> "\$STUB_LOG"
case "\$*" in
    "tauri --version") echo "tauri-cli 2.11.0" ;;
    "tauri build"*)
        [[ "\${STUB_CARGO_FAIL:-0}" == 1 ]] && exit 101
        mkdir -p "$DEB_DIR" && : > "$DEB_DIR/BlackBox Setup_${SRC_VER}_amd64.deb" ;;
esac
EOF
cat > "$STUB_BIN/npm" <<'EOF'
#!/usr/bin/env bash
echo "npm[user=${STUB_SUDO_USER:-} home=$HOME] $*" >> "$STUB_LOG"
EOF
chmod +x "$STUB_BIN/cargo" "$STUB_BIN/npm"

export REAL_USER REAL_HOME BLACKBOX_ROOT="$ROOT"
REAL_USER="$(id -un)"
REAL_HOME="$WORK/home"
mkdir -p "$REAL_HOME"

no_unprivileged_toolchain() {  # every cargo/npm call went through sudo -u $REAL_USER with its HOME
    local bad
    bad="$(grep -E '^(cargo|npm)\[' "$STUB_LOG" | grep -vF "[user=$REAL_USER home=$REAL_HOME]" || true)"
    [[ -z "$bad" ]] || fail "$1: the toolchain ran as the invoking user (root under 'sudo install.sh'), not $REAL_USER: $bad"
}

# ── 1. build_tauri_setup runs the toolchain as the real user ──────────────────
FUNC_FILE="$WORK/build.sh"
extract build_tauri_setup
: > "$STUB_LOG"
# shellcheck source=/dev/null
( set -euo pipefail; source "$FUNC_FILE"; build_tauri_setup ) > "$WORK/build.log" 2>&1 \
    || fail "1: build_tauri_setup failed: $(tail -5 "$WORK/build.log")"
stub_log_has '^cargo\[.*\] tauri build --bundles deb$' || fail "1: cargo tauri build never ran"
no_unprivileged_toolchain 1
stub_log_has '^apt install -y .*libwebkit2gtk-4\.1-dev' || fail "1: the apt build deps are no longer installed"

# ── 2-5. the Step 5 decision and install ─────────────────────────────────────
FUNC_FILE="$WORK/step5.sh"
for fn in tauri_source_version setup_app_package setup_app_build_reason \
          build_tauri_setup setup_app_deb install_setup_app_deb install_setup_app; do
    extract "$fn"
done
grep -q '^SETUP_APP_BIN=/usr/bin/blackbox-setup$' "$INSTALL_SH" || fail "SETUP_APP_BIN is not /usr/bin/blackbox-setup"

run5() {  # <case> — runs install_setup_app under set -e; prints the exit code
    : > "$STUB_LOG"
    rm -rf "$DEB_DIR"
    # shellcheck source=/dev/null disable=SC2034  # SETUP_APP_BIN is read by the sourced functions
    ( set -euo pipefail; SETUP_APP_BIN=/usr/bin/blackbox-setup; source "$FUNC_FILE"; install_setup_app ) \
        > "$WORK/$1.log" 2>&1
    echo $?
}
built() { stub_log_has '^cargo\[.*\] tauri build'; }
installed_deb() { stub_log_has "^apt install -y $DEB_DIR/"; }

# 2. current
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION="$SRC_VER" BLACKBOX_BUILD_TAURI='' run5 current)"
[[ "$rc" == 0 ]] || fail "2: rc=$rc $(cat "$WORK/current.log")"
built && fail "2: an installed setup app at the source version ($SRC_VER) was rebuilt"
installed_deb && fail "2: a .deb was installed although nothing was built"

# 3. version change / missing / forced / disabled
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 BLACKBOX_BUILD_TAURI='' run5 older)"
[[ "$rc" == 0 ]] || fail "3: rc=$rc $(cat "$WORK/older.log")"
built || fail "3: a version change (0.0.1 -> $SRC_VER) did not rebuild"
installed_deb || fail "3: the rebuilt .deb was not installed"
no_unprivileged_toolchain "3 (version change)"
rc="$(STUB_DPKG_OWNER='' STUB_DPKG_VERSION='' BLACKBOX_BUILD_TAURI='' run5 missing)"
[[ "$rc" == 0 ]] || fail "3: rc=$rc $(cat "$WORK/missing.log")"
built || fail "3: a box with no setup app (fresh install) did not build it"
installed_deb || fail "3: fresh-box .deb not installed"
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION="$SRC_VER" BLACKBOX_BUILD_TAURI=1 run5 forced)"
[[ "$rc" == 0 ]] || fail "3: rc=$rc $(cat "$WORK/forced.log")"
built || fail "3: BLACKBOX_BUILD_TAURI=1 did not rebuild"
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 BLACKBOX_BUILD_TAURI=0 run5 disabled)"
[[ "$rc" == 0 ]] || fail "3: rc=$rc $(cat "$WORK/disabled.log")"
built && fail "3: BLACKBOX_BUILD_TAURI=0 still built"

# 4. renamed package
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 STUB_DEB_PACKAGE=ai-black-box-flight-recorder-agent-setup BLACKBOX_BUILD_TAURI='' run5 renamed)"
[[ "$rc" == 0 ]] || fail "4: rc=$rc $(cat "$WORK/renamed.log")"
stub_log_has '^apt-get remove -y black-box-setup$' || fail "4: the old black-box-setup package was not removed before installing the renamed one"
REMOVE_AT="$(grep -n '^apt-get remove' "$STUB_LOG" | head -n1 | cut -d: -f1)"
INSTALL_AT="$(grep -n "^apt install -y $DEB_DIR/" "$STUB_LOG" | head -n1 | cut -d: -f1)"
[[ -n "$INSTALL_AT" && "$REMOVE_AT" -lt "$INSTALL_AT" ]] || fail "4: the old package must be removed before the new .deb is installed"
rc="$(STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 STUB_DEB_PACKAGE=black-box-setup BLACKBOX_BUILD_TAURI='' run5 same-name)"
stub_log_has '^apt-get remove' && fail "4: a same-name upgrade removed the installed package"

# 5. artifacts owned by someone else / failed builds
# The ownership guard must walk the whole tree. A test cannot create a file
# owned by another uid, so pin the walk itself: no depth limit on the find.
GUARD="$(sed -n '/^build_tauri_setup()[[:space:]]*{/,/^}/p' "$INSTALL_SH" | grep -E 'find .*! -user "\$REAL_USER"' || true)"
[[ -n "$GUARD" ]] || fail "5: build_tauri_setup no longer checks build-tree ownership with find ! -user"
[[ "$GUARD" == *maxdepth* ]] \
    && fail "5: the ownership guard stops at a fixed depth and misses root-owned release/deps/ and bundle/deb/<pkg>/: $GUARD"
# Everything the test creates is owned by the invoking user, so make the real
# user someone else — never the invoking user (root in a CI container).
FOREIGN_USER=root
[[ "$(id -u)" -eq 0 ]] && FOREIGN_USER=nobody
mkdir -p "$ROOT/installer/src-tauri/target/release/deps"
rc="$(REAL_USER="$FOREIGN_USER" STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 BLACKBOX_BUILD_TAURI='' run5 foreign-target)"
[[ "$rc" == 0 ]] || fail "5: a foreign-owned target/ with an installed app must not fail the install (rc=$rc)"
built && fail "5: built over a target/ owned by another user"
grep -q 'chown -R' "$WORK/foreign-target.log" || fail "5: the foreign-owned target/ is not reported with the chown remedy"
rm -rf "$ROOT/installer/src-tauri/target"
rc="$(STUB_CARGO_FAIL=1 STUB_DPKG_OWNER=black-box-setup STUB_DPKG_VERSION=0.0.1 BLACKBOX_BUILD_TAURI='' run5 fail-upgrade)"
[[ "$rc" == 0 ]] || fail "5: a failed rebuild with the app installed aborted the install (rc=$rc)"
grep -q 'WARN' "$WORK/fail-upgrade.log" || fail "5: a failed rebuild was not reported"
rc="$(STUB_CARGO_FAIL=1 STUB_DPKG_OWNER='' STUB_DPKG_VERSION='' BLACKBOX_BUILD_TAURI='' run5 fail-fresh)"
[[ "$rc" != 0 ]] || fail "5: a fresh box whose setup-app build failed reported success"

echo "ALL TESTS PASSED"
exit 0
