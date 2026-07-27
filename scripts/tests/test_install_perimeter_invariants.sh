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
# /etc/blackbox/root is the pointer blackbox-apt-install trusts AHEAD of its
# templated default. A service that could write it would redirect the allowlist
# lookup at a tree it controls and turn the bounded
# `NOPASSWD: /usr/local/sbin/blackbox-apt-install *` grant into
# `apt-get install -y <anything>` as root. The directory already holds
# service-user-owned zellij cert/key files, so "let the service rotate its own
# certs" is the plausible way a future change breaches this by accident.
while IFS= read -r line; do
    case "$line" in
        *"/etc/blackbox"*)
            fail "a generated unit grants ReadWritePaths on /etc/blackbox — that directory holds the apt-helper root pointer: $line" ;;
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

echo "ALL TESTS PASSED"
exit 0
