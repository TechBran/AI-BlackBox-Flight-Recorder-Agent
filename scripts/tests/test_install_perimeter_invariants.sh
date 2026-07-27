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
# `NOPASSWD: /usr/local/sbin/blackbox-apt-install *` grant into
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
# security model of that wildcard-granted helper is that the DESTINATION is
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
# install.sh as root. The whole security model of the wildcard-granted helper is
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

echo "ALL TESTS PASSED"
exit 0
