#!/usr/bin/env bash
# blackbox-write-systemd — root-owned dispatch helper for the update pipeline.
#
# WHY: The update flow needs to rewrite /etc/systemd/system/blackbox.service,
# the override.conf drop-in, the cli-agent-overrides drop-in, and the sudoers
# file whenever install.sh's generators change. Granting blackbox.service
# NOPASSWD sudo for `tee /etc/sudoers.d/blackbox-*` would let any
# prompt-injection RCE write arbitrary sudoers rules (e.g.,
# "bbx ALL=(root) NOPASSWD: ALL") = root pwn instantly.
#
# Instead, this helper accepts a (target_kind, source_file) pair where:
#   - target_kind is whitelisted to known values
#   - destination is HARDCODED per target_kind (caller cannot specify)
#   - sudoers writes are validated by `visudo -c` BEFORE the copy fires
#   - daemon-reload runs automatically after systemd-type writes
#
# Invoked via NOPASSWD sudoers grant:
#   bbx ALL=(root) NOPASSWD: /usr/local/sbin/blackbox-write-systemd *
#
# NO INSTALL ROOT IS BAKED IN HERE, deliberately: the source file arrives as
# an argument and every destination is hardcoded per target_kind. That is why
# the stale-helper failure class (customer, 2026-07-27 — see
# blackbox-apt-install's root-resolution block) cannot reach this helper. If a
# future edit ever needs $BLACKBOX_ROOT, use BLACKBOX_ROOT_PLACEHOLDER;
# install.sh Step 0b already substitutes it for both helpers.
#
# Usage:
#   sudo blackbox-write-systemd <target_kind> <source_file>
#
# Valid target_kind values:
#   unit                  → /etc/systemd/system/blackbox.service
#   override              → /etc/systemd/system/blackbox.service.d/override.conf
#   cli-agent-overrides   → /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf
#   zellij-web-unit       → /etc/systemd/system/zellij-web.service
#   models-unit           → /etc/systemd/system/blackbox-models.service
#   sudoers-system        → /etc/sudoers.d/blackbox-system
#   apt-install-helper    → /usr/local/sbin/blackbox-apt-install     (0755)
#   write-systemd-helper  → /usr/local/sbin/blackbox-write-systemd   (0755)
#
# The two *-helper kinds let startup_assert_helpers_current() re-template a
# root-owned dispatch helper onto an EXISTING box on every service start —
# mirroring the sudoers hook — so a box whose baked-in install root went stale
# (customer, 2026-07-27 — stale helper after commit 5eabbe45) self-heals
# without SSH. As with every other kind, the DESTINATION IS HARDCODED here:
# the caller supplies only the source file and picks a kind, never a path.
# That is the whole security model — a wildcard-granted helper that let the
# caller name its own /usr/local/sbin target would be a root-write primitive.
#
# Exit codes:
#   0 — wrote (+ daemon-reloaded for systemd kinds)
#   2 — missing arguments
#   3 — source file does not exist
#   4 — unknown target_kind
#   5 — sudoers visudo -c check failed (refused to install broken sudoers)

set -euo pipefail

TARGET_KIND="${1:-}"
SOURCE_FILE="${2:-}"

if [[ -z "$TARGET_KIND" || -z "$SOURCE_FILE" ]]; then
    echo "[blackbox-write-systemd] ERROR: usage: $0 <target_kind> <source_file>" >&2
    exit 2
fi

if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "[blackbox-write-systemd] ERROR: source file does not exist: $SOURCE_FILE" >&2
    exit 3
fi

# Target whitelist → HARDCODED destination + kind. Caller cannot influence the
# destination path; only chooses which of the supported targets. KIND drives
# the mode + post-write action below (systemd → 0644 + daemon-reload; sudoers →
# 0440 + visudo; sbin → 0755, no reload).
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
    sudoers-system)
        DEST="/etc/sudoers.d/blackbox-system"; KIND=sudoers ;;
    apt-install-helper)
        DEST="/usr/local/sbin/blackbox-apt-install"; KIND=sbin ;;
    write-systemd-helper)
        DEST="/usr/local/sbin/blackbox-write-systemd"; KIND=sbin ;;
    *)
        echo "[blackbox-write-systemd] ERROR: unknown target_kind: $TARGET_KIND" >&2
        echo "[blackbox-write-systemd] (Valid: unit | override | cli-agent-overrides | zellij-web-unit | models-unit | sudoers-system | apt-install-helper | write-systemd-helper)" >&2
        exit 4
        ;;
esac

# Sudoers: validate syntax BEFORE we install. visudo -c is the canonical
# check; refusing to install a broken sudoers file prevents the customer
# from locking themselves out of sudo entirely.
if [[ "$KIND" == "sudoers" ]]; then
    if ! /usr/sbin/visudo -c -f "$SOURCE_FILE" >/dev/null 2>&1; then
        echo "[blackbox-write-systemd] ERROR: sudoers source failed visudo -c:" >&2
        /usr/sbin/visudo -c -f "$SOURCE_FILE" >&2 || true
        exit 5
    fi
fi

# Ensure dest dir exists (the .d/ dir may not exist on a fresh install).
mkdir -p "$(dirname "$DEST")"

# Atomic write via temp + rename. mv within the same filesystem is atomic.
TMPDEST="${DEST}.update-tmp"
cp "$SOURCE_FILE" "$TMPDEST"
chown root:root "$TMPDEST"

# Mode per kind: sudoers 0440, sbin dispatch helpers 0755 (executable — the
# same perimeter as install.sh installs them with), systemd files 0644.
case "$KIND" in
    sudoers) chmod 0440 "$TMPDEST" ;;
    sbin)    chmod 0755 "$TMPDEST" ;;
    *)       chmod 0644 "$TMPDEST" ;;
esac

mv "$TMPDEST" "$DEST"
echo "[blackbox-write-systemd] Wrote $DEST"

# Trigger daemon-reload for systemd-type writes so the unit changes pick up.
# Sudoers re-read on each sudo invocation; sbin helpers are plain scripts —
# neither needs a reload.
if [[ "$KIND" == "systemd" ]]; then
    /bin/systemctl daemon-reload
    echo "[blackbox-write-systemd] systemctl daemon-reload OK"
fi

exit 0
