#!/usr/bin/env bash
# Source-level invariants for Scripts/install.sh that no sandbox can exercise.
#
# Everything here guards a property whose failure mode is silent: the file still
# runs, the install still "works", and something load-bearing has quietly gone.
# Same sed/grep-over-the-source approach as test_install_asterisk_block.sh, for
# the same reason — install.sh has no main() guard, so it cannot be sourced.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"

# ── 1. /etc/blackbox must never be writable by the service ────────────────────
# install.sh-grant removal, 2026-07-27: /etc/blackbox/system-packages.txt is the
# root-owned apt allowlist blackbox-apt-install trusts as its SOLE source (it
# replaced the former /etc/blackbox/root pointer). A service that could write it
# would append any package and turn the bounded
# `NOPASSWD: /usr/local/sbin/blackbox-apt-install <pkg>` grant into
# `apt-get install -y <anything>` as root. The directory already holds
# service-user-owned zellij cert/key files, so "let the service rotate its own
# certs" is the plausible way a future change breaches this by accident.
while IFS= read -r line; do
    case "$line" in
        *"/etc/blackbox"*)
            fail "a generated unit grants ReadWritePaths on /etc/blackbox — that directory holds the root-owned apt allowlist: $line" ;;
    esac
done < <(grep '^ReadWritePaths=' "$INSTALL_SH")

# ── 2. Step 7's restart must not fire during an in-flight update ──────────────
# The update runner re-runs install.sh from inside the update (the systemd_regen
# phase, after apt/pip/mcp). KillMode=process means an unconditional
# `systemctl restart blackbox.service` here SIGTERMs the uvicorn process that is
# iterating the update generator: new code on disk, old dependencies, no
# `complete` event. The runner holds an exclusive flock on Manifest/update.lock
# for the whole update, so failing to take it is an exact in-progress test.
#
# Every assertion below is anchored on executable syntax, never on prose: the
# comment block explaining the guard mentions all the same words, and a comment
# must not be able to satisfy a safety assertion.
grep -qE '^UPDATE_LOCK=.*Manifest/update\.lock' "$INSTALL_SH" \
    || fail "install.sh no longer resolves Manifest/update.lock — the restart is unguarded and an in-flight update will be killed by its own install.sh re-run"
grep -qE '^[[:space:]]*if .*flock -n "\$UPDATE_LOCK" true' "$INSTALL_SH" \
    || fail "the update-lock guard no longer probes the lock with flock -n"
grep -qE '^sudo systemctl restart blackbox\.service' "$INSTALL_SH" \
    && fail "an UNGUARDED (top-level) service restart is back in install.sh"
grep -qE '^[[:space:]]+sudo systemctl restart blackbox\.service' "$INSTALL_SH" \
    || fail "the guarded restart branch is gone — install.sh never restarts the service"

# ── 3. the dispatch helpers must be installed before Step 1's apt run ─────────
# install.sh runs under `set -e`. When the helper install sat after Step 1, a
# package apt could not locate aborted the installer before the helpers were
# refreshed — i.e. the repair never reached the box that needed it (the update
# runner re-runs this script precisely to fix a helper that breaks apt).
HELPER_LINE="$(grep -n '^install_update_helpers$' "$INSTALL_SH" | head -1 | cut -d: -f1)"
# Anchored on the pipeline itself, not on prose: comments in this file quote the
# command too, and a comment must never satisfy an ordering assertion.
APT_LINE="$(grep -nE '^[[:space:]]*\| xargs sudo apt install -y' "$INSTALL_SH" | head -1 | cut -d: -f1)"
[[ -n "$HELPER_LINE" ]] || fail "install_update_helpers is never invoked"
[[ -n "$APT_LINE" ]] || fail "could not find Step 1's apt install line"
[[ "$HELPER_LINE" -lt "$APT_LINE" ]] \
    || fail "install_update_helpers runs at line $HELPER_LINE, after apt at line $APT_LINE — a failing apt step will strand a stale helper"

# ── 4. bash syntax still parses ───────────────────────────────────────────────
# Cheap, and the only thing standing between a typo in a block nothing sources
# and a customer's install dying at line 1.
bash -n "$INSTALL_SH" || fail "install.sh does not parse"

# ── 5. the write-systemd apt-install-helper target hardcodes its destination ──
# I3(b): startup_assert_helpers_current() re-templates the root-owned apt helper
# on an existing box via a NEW blackbox-write-systemd target_kind. The whole
# security model of that sudo-granted helper is that the DESTINATION is
# hardcoded per target_kind — the caller supplies a source file and a kind,
# never a path. A target that derived its /usr/local/sbin dest from an argument
# would be a root-write primitive. Anchor on the case arm, not on prose.
WS="$REPO_ROOT/installer/templates/blackbox-write-systemd.sh"
[[ -f "$WS" ]] || fail "blackbox-write-systemd.sh not found"
bash -n "$WS" || fail "blackbox-write-systemd.sh does not parse"
grep -qE '^[[:space:]]*apt-install-helper\)' "$WS" \
    || fail "blackbox-write-systemd lost the apt-install-helper target_kind — the startup self-heal has nothing to call"
grep -qE 'DEST="/usr/local/sbin/blackbox-apt-install"; KIND=sbin' "$WS" \
    || fail "apt-install-helper no longer hardcodes /usr/local/sbin/blackbox-apt-install (0755 sbin) — a caller could aim the write elsewhere"

# ── 6. the startup self-heal hook exists and uses that hardcoded target ───────
# Mirrors startup_assert_sudoers_current: renders the tracked apt-helper
# template with the real root, then re-installs via the granted
# blackbox-write-systemd apt-install-helper target. Without this, an existing
# box's stale helper only heals via a full update — the narrower truth I3(a)
# corrected the runbook to.
STARTUP="$REPO_ROOT/Orchestrator/startup.py"
grep -q 'def startup_assert_helpers_current' "$STARTUP" \
    || fail "startup_assert_helpers_current() is gone — existing boxes lose the restart-time helper self-heal"
grep -qF '"apt-install-helper"' "$STARTUP" \
    || fail "the startup helper hook no longer routes through the apt-install-helper target_kind"

# ── 7. every write-systemd target_kind hardcodes an /etc (or /usr/local/sbin) ──
#      destination — never derives it from a caller argument ─────────────────────
# install.sh-grant removal, 2026-07-27: the decomposed update regen dispatches
# a bounded write-systemd kind per changed artifact instead of re-running
# install.sh as root. The whole security model of the sudo-granted helper is
# that the DESTINATION is hardcoded per kind — a caller of the sudo grant picks a
# kind and supplies a source file, NEVER a path. A kind that read its dest from
# $1/$2 would be an arbitrary-root-write primitive. Assert each new kind's
# hardcoded destination is present verbatim.
declare -A EXPECTED_DEST=(
    [mcp-unit]='DEST="/etc/systemd/system/blackbox-mcp.service"'
    [ydotoold-unit]='DEST="/etc/systemd/system/ydotoold.service"'
    [time-sync-dropin]='DEST="/etc/systemd/system/blackbox.service.d/time-sync.conf"'
    [asterisk-dropin]='DEST="/etc/systemd/system/blackbox.service.d/asterisk.conf"'
    [logrotate]='DEST="/etc/logrotate.d/blackbox"'
    [asterisk-sudoers]='DEST="/etc/sudoers.d/blackbox-asterisk"'
)
for kind in "${!EXPECTED_DEST[@]}"; do
    grep -qE "^[[:space:]]*${kind}\)" "$WS" \
        || fail "write-systemd is missing the $kind target_kind arm"
    grep -qF "${EXPECTED_DEST[$kind]}" "$WS" \
        || fail "$kind no longer hardcodes ${EXPECTED_DEST[$kind]} — a caller could aim the write elsewhere"
done

# asterisk-sudoers must route through the visudo -c gate (KIND=sudoers), exactly
# like sudoers-system — a sudoers write that skipped visudo could brick sudo or
# install an attacker rule.
grep -qF 'DEST="/etc/sudoers.d/blackbox-asterisk"; KIND=sudoers' "$WS" \
    || fail "asterisk-sudoers is not KIND=sudoers — it would bypass the visudo -c syntax gate"

# The structural invariant: NO case arm derives its DEST from a positional
# argument. Every `DEST=` assignment must be a literal path (or the one sandbox
# re-prefix line, which prepends an ENV var, never $1/$2). If any DEST line ever
# references the caller's arguments, that is the escalation this whole file
# guards against.
while IFS= read -r line; do
    case "$line" in
        *'DEST="${BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-}${DEST}"'*) : ;;  # test-only sandbox prefix, env-sourced
        *'$1'*|*'$2'*|*'$3'*|*'$@'*|*'$SOURCE_FILE'*|*'$TARGET_KIND'*|*'${1'*|*'${2'*|*'${3'*)
            fail "a write-systemd DEST assignment reads from a caller argument — that is an arbitrary-root-write primitive: $line" ;;
    esac
done < <(grep -E '^[[:space:]]*DEST=' "$WS")

# ── 7b. write-systemd must read NO caller source into any write ───────────────
# install.sh-grant removal, 2026-07-27 (trusted-template-store design): THE
# invariant this whole pivot serves. The earlier helper copied a caller-supplied
# $SOURCE_FILE ($2) VERBATIM into a root-owned systemd unit — combined with the
# standing `systemctl restart` grants that was a full in-service→root primitive.
# The fix is that write-systemd takes NO caller source: it copies the ROOT-OWNED
# /etc/blackbox/templates/<kind> store entry. Prove the source-file plumbing is
# GONE — no $SOURCE_FILE variable survives, and nothing reads $2 into a write.
grep -q 'SOURCE_FILE' "$WS" \
    && fail "blackbox-write-systemd still references SOURCE_FILE — a caller source can reach a root-owned write, the escalation this change removes"
# The single copy into the destination must come from the store, never an arg.
grep -qE '^[[:space:]]*cp "\$STORE_FILE" "\$TMPDEST"' "$WS" \
    || fail "the write-systemd copy no longer sources \$STORE_FILE — it must copy the root-owned store entry, not a caller file"
# No positional argument may feed a cp/install/tee/mv/redirect write.
while IFS= read -r line; do
    case "$line" in
        *'cp '*'$2'*|*'cp '*'${2'*|*'install '*'$2'*|*'install '*'${2'*|\
        *'tee '*'$2'*|*'> "$2'*|*'>"$2'*)
            fail "a write-systemd write reads from positional \$2 (a caller source): $line" ;;
    esac
done < "$WS"
# The store path is a FIXED /etc path, never derived from a caller argument.
grep -qE '^[[:space:]]*STORE_FILE="\$\{BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-\}/etc/blackbox/templates/' "$WS" \
    || fail "the write-systemd store path is not the fixed /etc/blackbox/templates path (env-prefixed for the sandbox only)"

# ── 8. the sudoers template must NOT grant `bash … install.sh` ────────────────
# install.sh-grant removal, 2026-07-27: THE regression guard for the whole
# change. The removed grant —
#   REAL_USER … NOPASSWD: /usr/bin/bash <root>/Scripts/install.sh
# — was the only in-service→arbitrary-root escalation: a prompt-injection RCE in
# the service could `sudo -n bash install.sh` and run the entire installer as
# root. The update pipeline now dispatches bounded blackbox-write-systemd kinds
# per changed template instead (Orchestrator/update/runner.py::_privileged_regen).
# If any future edit reintroduces a `bash …/install.sh` (or `bash …/*.sh`) sudo
# grant to the template, that door is open again — fail loudly here. Anchored on
# a NOPASSWD grant line, so the explanatory comments above (which name install.sh)
# can never satisfy or trip this assertion.
SUDOERS_TMPL="$REPO_ROOT/installer/templates/sudoers-blackbox-system"
[[ -f "$SUDOERS_TMPL" ]] || fail "sudoers-blackbox-system template not found"
if grep -qE '^[^#].*NOPASSWD:.*/bash[[:space:]].*install\.sh' "$SUDOERS_TMPL"; then
    fail "the sudoers template reintroduced a 'bash … install.sh' NOPASSWD grant — that is the in-service→root escalation this change removed"
fi

# ── 9. Scripts/update.sh checks the trust pins before anything changes ────────
# 2026-10 audit rank 79 (X04-security-clients-infra-11): the origin is checked
# against the root-owned pin BEFORE the fetch, the release signature of the
# exact commit BEFORE the first stash/tag/reset, and the reset installs that
# SHA, never `origin/main` (which a hostile .git or a concurrent fetch can move
# after the check). Anchored on executable lines only.
UPDATE_SH="$REPO_ROOT/Scripts/update.sh"
bash -n "$UPDATE_SH" || fail "update.sh does not parse"
line_of() { grep -nE "$1" "$UPDATE_SH" | head -n1 | cut -d: -f1; }
ORIGIN_AT="$(line_of '^check_pinned_origin$')"
LATEST_AT="$(line_of '^if ! LATEST_SHA="\$\(remote_main_sha "\$ORIGIN_URL"\)"; then$')"
FETCH_AT="$(line_of '^if ! git fetch origin "\+refs/heads/main:refs/remotes/origin/main"')"
SIGN_AT="$(line_of '^verify_release_commit "\$LATEST_SHA"$')"
OBJECTS_AT="$(line_of '^if ! OBJECT_PROBLEMS="\$\(run_tree_check objects "\$LATEST_SHA"\)"; then$')"
TAG_AT="$(line_of '^git tag "\$PRE_TAG"$')"
SWAP_AT="$(line_of '^if ! keep_local_work "auto-stash-\$\{PRE_TAG\}" "\$LATEST_SHA"')"
RESET_AT="$(line_of '^git reset --hard "\$LATEST_SHA"$')"
TREE_AT="$(line_of '^if ! TREE_PROBLEMS="\$\(run_tree_check worktree "\$LATEST_SHA"\)"; then$')"
INSTALL_AT="$(line_of '^if ! sudo "\$BLACKBOX_ROOT/Scripts/install.sh"; then$')"
[[ -n "$ORIGIN_AT" && -n "$FETCH_AT" && "$ORIGIN_AT" -lt "$FETCH_AT" ]] \
    || fail "update.sh must check the pinned origin (line ${ORIGIN_AT:-none}) before it fetches (line ${FETCH_AT:-none})"
# The commit installed is main as the origin reports it (git ls-remote), asked
# after the origin check and before the fetch, never read back from the
# checkout's refs (a tag named origin/main shadows refs/remotes/origin/main).
[[ -n "$LATEST_AT" && "$ORIGIN_AT" -lt "$LATEST_AT" && "$LATEST_AT" -lt "$FETCH_AT" ]] \
    || fail "update.sh must take LATEST_SHA from remote_main_sha between the origin check and the fetch (line ${LATEST_AT:-none})"
grep -qE '(rev-parse|LATEST_SHA=).*origin/main' "$UPDATE_SH" \
    && fail "update.sh reads the commit to install from the checkout's origin/main ref again"
[[ -n "$SIGN_AT" && -n "$TAG_AT" && "$SIGN_AT" -lt "$TAG_AT" ]] \
    || fail "update.sh must verify the release signature (line ${SIGN_AT:-none}) before it tags, freezes or stashes (line ${TAG_AT:-none})"
[[ -n "$OBJECTS_AT" && "$SIGN_AT" -lt "$OBJECTS_AT" && "$OBJECTS_AT" -lt "$TAG_AT" ]] \
    || fail "update.sh must hash-check the commit's objects (line ${OBJECTS_AT:-none}) before it tags"
[[ -n "$SWAP_AT" ]] || fail "update.sh no longer resets to the verified \$LATEST_SHA"
# Root runs install.sh from the tree only once it is the commit, byte for byte.
[[ -n "$RESET_AT" && -n "$TREE_AT" && -n "$INSTALL_AT" && "$RESET_AT" -lt "$TREE_AT" && "$TREE_AT" -lt "$INSTALL_AT" ]] \
    || fail "update.sh must check the tree on disk (line ${TREE_AT:-none}) between the reset (line ${RESET_AT:-none}) and sudo install.sh (line ${INSTALL_AT:-none})"
# Every git call on the checkout goes without replace refs, hooks or fsmonitor.
grep -qxF 'GIT_SAFE=(--no-replace-objects --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false)' "$UPDATE_SH" \
    || fail "update.sh lost its GIT_SAFE options"
grep -qxF 'git() { command "${GIT_CMD[@]}" "${GIT_SAFE[@]}" "$@"; }' "$UPDATE_SH" \
    || fail "update.sh's git() wrapper no longer adds GIT_SAFE"
grep -qE '^[[:space:]]*git reset --hard origin/main' "$UPDATE_SH" \
    && fail "update.sh resets to origin/main again — it must reset to the verified \$LATEST_SHA"
grep -qE '^git reset --hard "\$LATEST_SHA"$' "$UPDATE_SH" \
    || fail "update.sh's code swap is not 'git reset --hard \"\$LATEST_SHA\"'"
grep -qE '^UPDATE_TRUST_DIR=/etc/blackbox$' "$UPDATE_SH" \
    || fail "update.sh's trust pins no longer come from the fixed /etc/blackbox"
grep -qE '\$\{UPDATE_TRUST_(DIR|UID):?[-=]' "$UPDATE_SH" \
    && fail "update.sh lets the environment move its trust pins (a \${UPDATE_TRUST_*:-…} default)"

# ── 10. the in-app runner checks the same pins, in the same places ─────────────
RUNNER="$REPO_ROOT/Orchestrator/update/runner.py"
R_ORIGIN="$(grep -n 'verify.check_origin' "$RUNNER" | head -n1 | cut -d: -f1)"
R_LATEST="$(grep -n 'git_ops.latest_origin_sha' "$RUNNER" | head -n1 | cut -d: -f1)"
R_FETCH="$(grep -n 'git_ops.fetch_origin_main' "$RUNNER" | head -n1 | cut -d: -f1)"
R_SIGN="$(grep -n 'verify.check_commit' "$RUNNER" | head -n1 | cut -d: -f1)"
R_OBJECTS="$(grep -n 'verify.check_objects' "$RUNNER" | head -n1 | cut -d: -f1)"
R_TAG="$(grep -n 'git_ops.tag, self.pre_update_tag' "$RUNNER" | head -n1 | cut -d: -f1)"
R_RESET="$(grep -n 'self._write_state(PHASE_RESET_HARD)' "$RUNNER" | head -n1 | cut -d: -f1)"
R_TREE="$(grep -n 'verify.check_worktree' "$RUNNER" | head -n1 | cut -d: -f1)"
R_APT="$(grep -n 'self._write_state(PHASE_APT_INSTALL)' "$RUNNER" | head -n1 | cut -d: -f1)"
[[ -n "$R_ORIGIN" && -n "$R_FETCH" && "$R_ORIGIN" -lt "$R_FETCH" ]] \
    || fail "runner.py must check the pinned origin before its fetch"
[[ -n "$R_LATEST" && "$R_ORIGIN" -lt "$R_LATEST" && "$R_LATEST" -lt "$R_FETCH" ]] \
    || fail "runner.py must ask the origin for main (latest_origin_sha) between the origin check and the fetch"
[[ -n "$R_SIGN" && -n "$R_TAG" && "$R_SIGN" -lt "$R_TAG" ]] \
    || fail "runner.py must verify the release signature before it tags"
[[ -n "$R_OBJECTS" && "$R_OBJECTS" -lt "$R_TAG" ]] \
    || fail "runner.py must hash-check the commit's objects before it tags"
[[ -n "$R_RESET" && -n "$R_TREE" && -n "$R_APT" && "$R_RESET" -lt "$R_TREE" && "$R_TREE" -lt "$R_APT" ]] \
    || fail "runner.py must check the tree on disk after the reset and before anything is installed"
GIT_OPS="$REPO_ROOT/Orchestrator/update/git_ops.py"
grep -qF '["git", *GIT_SAFE_OPTIONS, *args]' "$GIT_OPS" \
    || fail "git_ops._git no longer passes GIT_SAFE_OPTIONS to every git call"
grep -qE 'rev-parse", "origin/main"' "$GIT_OPS" \
    && fail "git_ops reads the commit to install from the checkout's origin/main ref again"
grep -qE '^TRUST_DIR = "/etc/blackbox"$' "$REPO_ROOT/Orchestrator/update/verify.py" \
    || fail "verify.py's trust pins no longer come from the fixed /etc/blackbox"
grep -q 'os.environ' "$REPO_ROOT/Orchestrator/update/verify.py" \
    && fail "verify.py reads the environment — the service's environment comes partly from the service-writable .env"

echo "ALL TESTS PASSED"
exit 0
