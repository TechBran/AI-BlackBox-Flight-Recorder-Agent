#!/usr/bin/env bash
# blackbox-write-systemd — root-owned dispatch helper for the update pipeline.
#
# WHY / SECURITY MODEL (install.sh-grant removal, 2026-07-27):
# This helper takes NO caller-supplied source. For a whitelisted <target_kind>
# it copies a RENDERED, root-owned template from the trusted store
#   /etc/blackbox/templates/<target_kind>
# to that kind's HARDCODED destination. The service user controls NEITHER the
# destination NOR the CONTENT of any privileged write.
#
# This is the fix for the escalation the first cut left open: the earlier
# version copied a caller-supplied <source_file> VERBATIM into a root-owned
# systemd unit, with only a syntax-only `visudo -c` on the sudoers kind.
# Combined with the standing `systemctl restart` grants that was a full
# in-service→root primitive (write an `override` drop-in with User=root +
# ExecStartPre=… → restart → root) and it also defeated the /etc allowlist
# lockdown (write a drop-in granting ReadWritePaths=/etc/blackbox, restart,
# then poison the allowlist). Bounding only the destination is not enough — a
# helper that takes attacker-controllable CONTENT into a trust anchor IS the
# escalation. Closure is by CONSTRUCTION (root-owned store), not by content
# validation (a denylist on systemd/sudoers syntax is unwinnable).
#
# The store is populated ONLY by the human-run `sudo bash install.sh` publish
# step (publish_template_store()). /etc/blackbox is never in the service unit's
# ReadWritePaths (ProtectSystem=strict + the perimeter invariant test), so the
# running service cannot rewrite the store. The one repo→store bridge —
# install.sh — is no longer service-runnable once its NOPASSWD grant is removed.
#
# Invoked via NOPASSWD sudoers grant:
#   bbx ALL=(root) NOPASSWD: /usr/local/sbin/blackbox-write-systemd *
#
# Usage:
#   sudo blackbox-write-systemd <target_kind>
#
# A second positional argument is ACCEPTED-AND-IGNORED: earlier callers (the
# update runner and the startup hooks) passed a rendered <source_file> as $2.
# It is never read — the source of truth is the root-owned store above. Ignoring
# it keeps those callers working across the M0→M3 transition; once they are
# reworked to the no-source shape the extra arg simply stops being sent.
#
# NO INSTALL ROOT IS BAKED IN HERE, deliberately: the store path is a FIXED
# /etc path and every destination is hardcoded per target_kind. If a future
# edit ever needs $BLACKBOX_ROOT, use BLACKBOX_ROOT_PLACEHOLDER; install.sh
# Step 0b already substitutes it for both helpers.
#
# Valid target_kind values (destination is HARDCODED per kind, never an arg):
#   unit                  → /etc/systemd/system/blackbox.service
#   override              → /etc/systemd/system/blackbox.service.d/override.conf
#   cli-agent-overrides   → /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf
#   zellij-web-unit       → /etc/systemd/system/zellij-web.service
#   models-unit           → /etc/systemd/system/blackbox-models.service
#   mcp-unit              → /etc/systemd/system/blackbox-mcp.service
#   ydotoold-unit         → /etc/systemd/system/ydotoold.service
#   time-sync-dropin      → /etc/systemd/system/blackbox.service.d/time-sync.conf
#   asterisk-dropin       → /etc/systemd/system/blackbox.service.d/asterisk.conf
#   logrotate             → /etc/logrotate.d/blackbox            (0644, no reload)
#   sudoers-system        → /etc/sudoers.d/blackbox-system       (0440, visudo -c)
#   asterisk-sudoers      → /etc/sudoers.d/blackbox-asterisk     (0440, visudo -c)
#   apt-install-helper    → /usr/local/sbin/blackbox-apt-install     (0755)
#   write-systemd-helper  → /usr/local/sbin/blackbox-write-systemd   (0755)
#
# Whether a given kind has a store entry is decided by the installer's publish
# step: kinds backed by a git-tracked template (sudoers-system,
# apt-install-helper, write-systemd-helper, zellij-web-unit) are published;
# the inline-heredoc units in install.sh (blackbox.service, override,
# cli-agent-overrides, mcp, ydotoold, time-sync, logrotate) and the
# asterisk-* kinds are NOT — dispatching one of those FAILS CLOSED (exit 6)
# naming `sudo bash install.sh`, which writes them directly as root. That is
# the accepted operator-step trade, not a regression: nothing dispatches those
# kinds today.
#
# TEST-ONLY sandbox: if $BLACKBOX_WRITE_SYSTEMD_DEST_ROOT is set, it is prefixed
# onto BOTH the store-read path AND the hardcoded DEST, and the chown-root +
# daemon-reload steps are skipped — so the behaviour tests can populate a fake
# store and exercise every arm unprivileged against a mktemp tree. This is SAFE
# by construction: the NOPASSWD grant runs this helper under sudo, whose
# env_reset scrubs every variable not in env_keep — so the prefix is ALWAYS
# empty in production and both paths are exactly the /etc paths baked in below.
# It is an environment variable, never $1/$2, so it cannot let a caller of the
# sudo grant redirect the read or the write (same model as the apt helper's
# BLACKBOX_ROOT resolution).
#
# The two *-helper kinds let startup_assert_helpers_current() re-assert a
# root-owned dispatch helper onto an EXISTING box on every service start —
# mirroring the sudoers hook — from the trusted store, so a box whose deployed
# helper drifts from the last human-published store entry self-heals without
# SSH. As with every kind, the DESTINATION and the STORE SOURCE are both fixed
# here: the caller supplies only a kind, never a path or content.
#
# Exit codes:
#   0 — wrote (+ daemon-reloaded for systemd kinds)
#   2 — missing target_kind
#   4 — unknown target_kind
#   5 — sudoers store entry failed visudo -c (refused to install broken sudoers)
#   6 — store entry missing (run `sudo bash install.sh` to publish the store)

set -euo pipefail

TARGET_KIND="${1:-}"

if [[ -z "$TARGET_KIND" ]]; then
    echo "[blackbox-write-systemd] ERROR: usage: $0 <target_kind>" >&2
    exit 2
fi

# A second (or later) argument is a legacy caller passing a now-unused source
# file. Never read it; note it so a stray call is visible in the journal. This
# helper copies ONLY the root-owned store entry — never a caller source.
if [[ $# -gt 1 ]]; then
    echo "[blackbox-write-systemd] NOTE: ignoring extra argument(s) — this helper takes only <target_kind> and copies the root-owned template store, never a caller-supplied source." >&2
fi

# Target whitelist → HARDCODED destination + kind. Caller cannot influence the
# destination path; only chooses which of the supported targets. KIND drives
# the mode + post-write action below (systemd → 0644 + daemon-reload; sudoers →
# 0440 + visudo; sbin → 0755, no reload; plain → 0644, no reload).
case "$TARGET_KIND" in
    unit)
        DEST="/etc/systemd/system/blackbox.service"; KIND=systemd ;;
    override)
        DEST="/etc/systemd/system/blackbox.service.d/override.conf"; KIND=systemd ;;
    cli-agent-overrides)
        DEST="/etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf"; KIND=systemd ;;
    zellij-web-unit)
        DEST="/etc/systemd/system/zellij-web.service"; KIND=systemd ;;
    models-unit)
        DEST="/etc/systemd/system/blackbox-models.service"; KIND=systemd ;;
    mcp-unit)
        DEST="/etc/systemd/system/blackbox-mcp.service"; KIND=systemd ;;
    ydotoold-unit)
        DEST="/etc/systemd/system/ydotoold.service"; KIND=systemd ;;
    time-sync-dropin)
        DEST="/etc/systemd/system/blackbox.service.d/time-sync.conf"; KIND=systemd ;;
    asterisk-dropin)
        DEST="/etc/systemd/system/blackbox.service.d/asterisk.conf"; KIND=systemd ;;
    logrotate)
        # Plain config file — 0644, NO daemon-reload (logrotate re-reads
        # /etc/logrotate.d/* on each cron run; nothing to signal).
        DEST="/etc/logrotate.d/blackbox"; KIND=plain ;;
    sudoers-system)
        DEST="/etc/sudoers.d/blackbox-system"; KIND=sudoers ;;
    asterisk-sudoers)
        # Asterisk boxes only. visudo -c gated exactly like sudoers-system.
        DEST="/etc/sudoers.d/blackbox-asterisk"; KIND=sudoers ;;
    apt-install-helper)
        DEST="/usr/local/sbin/blackbox-apt-install"; KIND=sbin ;;
    write-systemd-helper)
        DEST="/usr/local/sbin/blackbox-write-systemd"; KIND=sbin ;;
    *)
        echo "[blackbox-write-systemd] ERROR: unknown target_kind: $TARGET_KIND" >&2
        echo "[blackbox-write-systemd] (Valid: unit | override | cli-agent-overrides | zellij-web-unit | models-unit | mcp-unit | ydotoold-unit | time-sync-dropin | asterisk-dropin | logrotate | sudoers-system | asterisk-sudoers | apt-install-helper | write-systemd-helper)" >&2
        exit 4
        ;;
esac

# Trusted root-owned template store. Populated ONLY by the human-run
# `sudo bash install.sh` publish step; the running service has no write path to
# /etc/blackbox (ProtectSystem=strict + perimeter invariant). $TARGET_KIND has
# already passed the whitelist case above — it is one of the literal kinds, so
# it can never traverse out of the store directory. The
# $BLACKBOX_WRITE_SYSTEMD_DEST_ROOT prefix is the TEST-ONLY sandbox rebase (empty
# in production, sourced from an env var, never from $1/$2 — see header).
STORE_FILE="${BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-}/etc/blackbox/templates/${TARGET_KIND}"

# TEST-ONLY sandbox prefix (see header). Empty in production because sudo's
# env_reset scrubs it; when the behaviour tests set it, the whole write lands
# under a mktemp tree and privileged steps are skipped. The DEST above is still
# the hardcoded per-kind path — this only relocates the whole tree, it never
# comes from a caller argument.
DEST="${BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-}${DEST}"

# Fail closed if the store entry is absent (store not yet published on a fresh
# box, or this kind is an install.sh-only inline-heredoc / asterisk artifact).
# Never fabricate content — name the one command that publishes the store.
if [[ ! -f "$STORE_FILE" ]]; then
    echo "[blackbox-write-systemd] ERROR: trusted template store entry missing: $STORE_FILE" >&2
    echo "[blackbox-write-systemd] The store is published only by the human-run installer;" >&2
    echo "[blackbox-write-systemd] this kind has no store entry on this box. Run" >&2
    echo "[blackbox-write-systemd] \`sudo bash install.sh\` to (re)publish it (or, for an" >&2
    echo "[blackbox-write-systemd] inline-heredoc unit, install.sh writes it directly)." >&2
    exit 6
fi

# Sudoers: validate the ROOT-OWNED store entry's SYNTAX before we install.
# visudo -c is the canonical check; refusing to install a broken sudoers file
# prevents ever locking the box out of sudo. Belt-and-braces — the store is
# already trusted, but a bad rule must never reach /etc/sudoers.d.
if [[ "$KIND" == "sudoers" ]]; then
    if ! /usr/sbin/visudo -c -f "$STORE_FILE" >/dev/null 2>&1; then
        echo "[blackbox-write-systemd] ERROR: sudoers store entry failed visudo -c:" >&2
        /usr/sbin/visudo -c -f "$STORE_FILE" >&2 || true
        exit 5
    fi
fi

# Ensure dest dir exists (the .d/ dir may not exist on a fresh install).
mkdir -p "$(dirname "$DEST")"

# Atomic write via temp + rename. mv within the same filesystem is atomic. The
# source is ALWAYS the root-owned store entry — never a caller-supplied file.
TMPDEST="${DEST}.update-tmp"
cp "$STORE_FILE" "$TMPDEST"
# chown to root is a privileged op — skip it under the test sandbox, which runs
# unprivileged. In production the prefix is empty and this always runs.
if [[ -z "${BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-}" ]]; then
    chown root:root "$TMPDEST"
fi

# Mode per kind: sudoers 0440, sbin dispatch helpers 0755 (executable — the
# same perimeter as install.sh installs them with), systemd + plain files 0644.
case "$KIND" in
    sudoers) chmod 0440 "$TMPDEST" ;;
    sbin)    chmod 0755 "$TMPDEST" ;;
    *)       chmod 0644 "$TMPDEST" ;;
esac

mv "$TMPDEST" "$DEST"
echo "[blackbox-write-systemd] Wrote $DEST (from $STORE_FILE)"

# Trigger daemon-reload for systemd-type writes so the unit changes pick up.
# Sudoers re-read on each sudo invocation; plain configs (logrotate) and sbin
# helpers are inert files — none needs a reload. Skipped under the test sandbox.
if [[ "$KIND" == "systemd" && -z "${BLACKBOX_WRITE_SYSTEMD_DEST_ROOT:-}" ]]; then
    /bin/systemctl daemon-reload
    echo "[blackbox-write-systemd] systemctl daemon-reload OK"
fi

exit 0
