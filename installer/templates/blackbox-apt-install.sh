#!/usr/bin/env bash
# blackbox-apt-install — root-owned dispatch helper for the update pipeline.
#
# WHY: The update flow needs to install new apt packages whenever a commit
# changes Scripts/onboarding/system-packages.txt. Granting blackbox.service
# NOPASSWD sudo for `apt-get install -y *` would let any prompt-injection
# RCE through any MCP tool install arbitrary root postinst code = full pwn.
# Instead, this helper is the ONLY thing sudo lets through, and it validates
# the requested package name against:
#   1. POSIX-safe regex ^[a-z0-9.+-]+$  (no shell metacharacters)
#   2. Membership in the MUST_HAVE+SHOULD_HAVE allowlist parsed from
#      $BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt
#
# Both checks must pass. The allowlist file is root-readable but
# customer-non-writable (since it lives in $BLACKBOX_ROOT which is
# customer-owned but only modified via `git reset --hard` during updates).
#
# Invoked via NOPASSWD sudoers grant:
#   bbx ALL=(root) NOPASSWD: /usr/local/sbin/blackbox-apt-install *
#
# Usage:
#   sudo blackbox-apt-install <package-name>
#
# Exit codes:
#   0 — installed successfully (or already installed; apt is idempotent)
#   2 — missing package argument
#   3 — package name failed regex check
#   4 — allowlist file unreadable
#   5 — package not in allowlist

set -euo pipefail

PACKAGE="${1:-}"
# BLACKBOX_ROOT defaults to the canonical Track 4 customer install path.
# Caller (update runner) can override via env var if needed for testing,
# but sudo strips env by default — the default is what gets used in prod.
BLACKBOX_ROOT="${BLACKBOX_ROOT:-/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main}"
ALLOWLIST_FILE="${BLACKBOX_ROOT}/Scripts/onboarding/system-packages.txt"

if [[ -z "$PACKAGE" ]]; then
    echo "[blackbox-apt-install] ERROR: no package specified" >&2
    echo "Usage: blackbox-apt-install <package-name>" >&2
    exit 2
fi

# Regex check — POSIX package names only. Rejects spaces, semicolons,
# pipes, redirects, $(), backticks, anything that could be argv injection.
if ! [[ "$PACKAGE" =~ ^[a-z0-9.+-]+$ ]]; then
    echo "[blackbox-apt-install] ERROR: invalid package name: $PACKAGE" >&2
    echo "[blackbox-apt-install] (Must match ^[a-z0-9.+-]+\$)" >&2
    exit 3
fi

if [[ ! -r "$ALLOWLIST_FILE" ]]; then
    echo "[blackbox-apt-install] ERROR: allowlist file not readable: $ALLOWLIST_FILE" >&2
    exit 4
fi

# Parse allowlist — same grep pattern install.sh Step 1 uses to install the
# initial set. Format: `<package>  # <bucket> # <reason>`. Buckets MUST_HAVE
# and SHOULD_HAVE both pass.
ALLOWED=$(grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' "$ALLOWLIST_FILE" | awk '{print $1}')

# Fixed-string + exact-line match. -F disables regex, -x requires whole line.
if ! echo "$ALLOWED" | grep -qFx "$PACKAGE"; then
    echo "[blackbox-apt-install] ERROR: package not in allowlist: $PACKAGE" >&2
    echo "[blackbox-apt-install] (Edit $ALLOWLIST_FILE to add)" >&2
    exit 5
fi

echo "[blackbox-apt-install] Installing $PACKAGE (allowlisted)..."
/usr/bin/apt-get install -y --no-install-recommends "$PACKAGE"
echo "[blackbox-apt-install] $PACKAGE installed."
exit 0
