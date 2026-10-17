#!/usr/bin/env bash
# Test for install.sh Step 2c's zellij download scratch directory.
#
# 2026-10-09 audit X06-ops-deploy-07 / O22-01 (rank 40): the download block
# declared `local TMPDIR`, which shadows the environment variable mktemp and
# every child tool read, and registered `trap 'rm -rf "${TMPDIR:-}"' EXIT` that
# was never cleared. Two failure modes followed:
#   1. with TMPDIR exported (CI, sandboxed shells), the shadow is exported too,
#      so once the download dir is removed every later `mktemp` in the step
#      fails and set -e aborts the install;
#   2. the trap outlives the function, so whenever the shell exits after Step 2c
#      returned it expands the CALLER's TMPDIR and rm -rf's that tree.
#
# Proves, against the real step_2c_install_zellij() extracted from install.sh
# and run in a mktemp sandbox (PATH stubs for sudo/curl/systemctl; the binary
# destination rewritten into the sandbox; nothing under /etc or /usr touched):
#   A. exported TMPDIR + a good download: the step completes, writes config.kdl,
#      removes its scratch dir, leaves no EXIT trap, and the caller's TMPDIR
#      survives;
#   B. exported TMPDIR + a failed download: the step fails, the scratch dir is
#      removed and the caller's TMPDIR survives;
#   C. a caller-scope TMPDIR (not exported): it survives the shell's exit.
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

# The step reads its pin + unit template from $BLACKBOX_ROOT, and reconciles
# $BLACKBOX_ROOT/Orchestrator/cli_agent/state/zellij_sessions.json — so point it
# at a sandbox root, never at a live checkout whose session file it could wipe.
SANDBOX_ROOT="$WORK/root"
mkdir -p "$SANDBOX_ROOT/installer/templates"
cp "$REPO_ROOT/installer/templates/zellij-version" \
   "$REPO_ROOT/installer/templates/zellij-web.service" \
   "$SANDBOX_ROOT/installer/templates/"
ZELLIJ_VERSION="$(grep -vE '^[[:space:]]*(#|$)' "$SANDBOX_ROOT/installer/templates/zellij-version" | head -n 1 | tr -d '[:space:]')"

# Extract the function. Its config heredocs contain a column-0 `}`, so the usual
# `/^fn() {/,/^}/` range would stop early: take everything up to the call line.
FUNC_FILE="$WORK/func.sh"
awk '/^step_2c_install_zellij\(\)[[:space:]]*\{/{on=1} /^step_2c_install_zellij$/{on=0} on' \
    "$INSTALL_SH" > "$FUNC_FILE"
grep -q '^step_2c_install_zellij()' "$FUNC_FILE" || fail "could not extract step_2c_install_zellij() from install.sh"
FAKE_BIN="$WORK/fakeroot/usr/local/bin/zellij"
mkdir -p "$(dirname "$FAKE_BIN")"
grep -q 'local ZELLIJ_BIN="/usr/local/bin/zellij"' "$FUNC_FILE" \
    || fail "step_2c no longer declares ZELLIJ_BIN=/usr/local/bin/zellij (update this test)"
sed -i "s|local ZELLIJ_BIN=\"/usr/local/bin/zellij\"|local ZELLIJ_BIN=\"$FAKE_BIN\"|" "$FUNC_FILE"

# curl handler: serve a fake release (a zellij that reports the pinned version)
# and its sha256sum, recording each --output path so the test can find the
# scratch dir the step downloaded into.
FAKE_RELEASE="$WORK/release"
mkdir -p "$FAKE_RELEASE"
printf '#!/bin/sh\necho "zellij %s"\n' "$ZELLIJ_VERSION" > "$FAKE_RELEASE/zellij"
chmod +x "$FAKE_RELEASE/zellij"
tar -czf "$FAKE_RELEASE/zellij.tar.gz" -C "$FAKE_RELEASE" zellij
SHA="$(sha256sum "$FAKE_RELEASE/zellij" | awk '{print $1}')"
cat > "$WORK/curl-ok" <<EOF
#!/usr/bin/env bash
echo "\$2" >> "$WORK/outputs.txt"
case "\$1" in
    *.tar.gz)    cp "$FAKE_RELEASE/zellij.tar.gz" "\$2" ;;
    *.sha256sum) echo "$SHA  zellij" > "\$2" ;;
    *) exit 22 ;;
esac
EOF
cat > "$WORK/curl-fail" <<EOF
#!/usr/bin/env bash
echo "\$2" >> "$WORK/outputs.txt"
exit 22
EOF
chmod +x "$WORK/curl-ok" "$WORK/curl-fail"

export REAL_USER REAL_HOME BLACKBOX_ROOT
REAL_USER="$(id -un)"
BLACKBOX_ROOT="$SANDBOX_ROOT"

# run_step <case-dir> <curl-handler> <export|caller> — runs the step in a child
# bash under install.sh's `set -euo pipefail`, with a caller TMPDIR holding a
# sentinel. Prints the child's exit code.
run_step() {
    local case_dir="$1" handler="$2" scope="$3"
    mkdir -p "$case_dir/caller-tmp" "$case_dir/home"
    : > "$case_dir/caller-tmp/sentinel"
    rm -f "$WORK/outputs.txt" "$FAKE_BIN"
    REAL_HOME="$case_dir/home" STUB_CURL_HANDLER="$handler" \
    CASE_DIR="$case_dir" SCOPE="$scope" FUNC_FILE="$FUNC_FILE" \
        bash -c '
            set -euo pipefail
            if [[ "$SCOPE" == export ]]; then
                export TMPDIR="$CASE_DIR/caller-tmp"
            else
                unset TMPDIR
                TMPDIR="$CASE_DIR/caller-tmp"
            fi
            source "$FUNC_FILE"
            step_2c_install_zellij
            trap -p EXIT > "$CASE_DIR/trap-after.txt"
        ' > "$case_dir/run.log" 2>&1
    echo $?
}

scratch_dirs() { [[ -f "$WORK/outputs.txt" ]] && xargs -r -n1 dirname < "$WORK/outputs.txt" | sort -u; }

# ── A. exported TMPDIR, good download ─────────────────────────────────────────
A="$WORK/case-a"
rc="$(run_step "$A" "$WORK/curl-ok" export)"
[[ "$rc" == 0 ]] || fail "A: step_2c failed with TMPDIR exported (rc=$rc) — the TMPDIR shadow broke a later mktemp: $(tail -5 "$A/run.log")"
[[ -f "$A/caller-tmp/sentinel" ]] || fail "A: the caller's TMPDIR was deleted"
[[ -x "$FAKE_BIN" ]] || fail "A: the downloaded zellij was not installed"
[[ -f "$A/home/.config/zellij/config.kdl" ]] || fail "A: config.kdl not written after the download"
[[ -s "$A/trap-after.txt" ]] && fail "A: step_2c left an EXIT trap behind: $(cat "$A/trap-after.txt")"
DLS="$(scratch_dirs)"
[[ -n "$DLS" ]] || fail "A: curl was never asked to download into a scratch dir"
while IFS= read -r d; do
    [[ "$d" == "$A/caller-tmp" ]] && fail "A: the download landed in the caller's TMPDIR"
    [[ -e "$d" ]] && fail "A: scratch dir $d was not removed"
done <<< "$DLS"

# ── B. exported TMPDIR, failed download ───────────────────────────────────────
B="$WORK/case-b"
rc="$(run_step "$B" "$WORK/curl-fail" export)"
[[ "$rc" != 0 ]] || fail "B: step_2c reported success although the download failed"
grep -q 'zellij tarball download failed' "$B/run.log" || fail "B: unexpected failure: $(tail -5 "$B/run.log")"
[[ -f "$B/caller-tmp/sentinel" ]] || fail "B: the failure path deleted the caller's TMPDIR"
while IFS= read -r d; do
    [[ -n "$d" && -e "$d" ]] && fail "B: scratch dir $d leaked on the failure path"
done <<< "$(scratch_dirs)"

# ── C. caller-scope TMPDIR survives the shell's exit ──────────────────────────
C="$WORK/case-c"
rc="$(run_step "$C" "$WORK/curl-ok" caller)"
[[ "$rc" == 0 ]] || fail "C: step_2c failed (rc=$rc): $(tail -5 "$C/run.log")"
[[ -f "$C/caller-tmp/sentinel" ]] || fail "C: an EXIT trap left by step_2c rm -rf'd the caller's TMPDIR when the shell exited"

# ── source-level: no function shadows TMPDIR ──────────────────────────────────
grep -nE '^[[:space:]]*(local|declare|typeset)[[:space:]]+([^#]*[[:space:]])?TMPDIR([=[:space:]]|$)' "$INSTALL_SH" \
    && fail "install.sh declares a local TMPDIR again (it shadows the env var mktemp reads)"
grep -nE '^[[:space:]]*TMPDIR=' "$INSTALL_SH" \
    && fail "install.sh assigns TMPDIR (use a script-specific name)"

echo "ALL TESTS PASSED"
exit 0
