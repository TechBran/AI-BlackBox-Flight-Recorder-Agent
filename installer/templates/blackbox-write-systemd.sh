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
# Usage:
#   sudo blackbox-write-systemd <target_kind> <source_file>
#
# Valid target_kind values:
#   unit                  → /etc/systemd/system/blackbox.service
#   override              → /etc/systemd/system/blackbox.service.d/override.conf
#   cli-agent-overrides   → /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf
#   zellij-web-unit       → /etc/systemd/system/zellij-web.service
#   sudoers-system        → /etc/sudoers.d/blackbox-system
#
# Exit codes:
#   0 — wrote + daemon-reloaded (or just wrote for sudoers)
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

# Target whitelist → HARDCODED destination. Caller cannot influence the
# destination path; only chooses which of the supported targets.
case "$TARGET_KIND" in
    unit)
        DEST="/etc/systemd/system/blackbox.service"
        IS_SUDOERS=0
        ;;
    override)
        DEST="/etc/systemd/system/blackbox.service.d/override.conf"
        IS_SUDOERS=0
        ;;
    cli-agent-overrides)
        DEST="/etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf"
        IS_SUDOERS=0
        ;;
    zellij-web-unit)
        DEST="/etc/systemd/system/zellij-web.service"
        IS_SUDOERS=0
        ;;
    sudoers-system)
        DEST="/etc/sudoers.d/blackbox-system"
        IS_SUDOERS=1
        ;;
    *)
        echo "[blackbox-write-systemd] ERROR: unknown target_kind: $TARGET_KIND" >&2
        echo "[blackbox-write-systemd] (Valid: unit | override | cli-agent-overrides | zellij-web-unit | sudoers-system)" >&2
        exit 4
        ;;
esac

# Sudoers: validate syntax BEFORE we install. visudo -c is the canonical
# check; refusing to install a broken sudoers file prevents the customer
# from locking themselves out of sudo entirely.
if [[ "$IS_SUDOERS" -eq 1 ]]; then
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

# Sudoers requires mode 0440, systemd files mode 0644.
if [[ "$IS_SUDOERS" -eq 1 ]]; then
    chmod 0440 "$TMPDEST"
else
    chmod 0644 "$TMPDEST"
fi

mv "$TMPDEST" "$DEST"
echo "[blackbox-write-systemd] Wrote $DEST"

# Trigger daemon-reload for systemd-type writes so the unit changes pick up.
# Sudoers don't need any reload — sudo re-reads /etc/sudoers.d/ on each invocation.
if [[ "$IS_SUDOERS" -eq 0 ]]; then
    /bin/systemctl daemon-reload
    echo "[blackbox-write-systemd] systemctl daemon-reload OK"
fi

exit 0
