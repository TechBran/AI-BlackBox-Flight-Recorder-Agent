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
#   2. Membership in the MUST_HAVE+SHOULD_HAVE allowlist read from the
#      ROOT-OWNED /etc/blackbox/system-packages.txt
#
# install.sh-grant removal, 2026-07-27: the allowlist is now read ONLY from
# /etc/blackbox/system-packages.txt — a root:root 0644 copy that install.sh
# (human-run, root) writes and is the ONLY writer of. This CLOSES the RCE gap
# the previous version documented as open. The old helper resolved the
# allowlist from $BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt, which
# lives inside blackbox.service's ReadWritePaths — so a prompt-injected service
# user could append `evil # MUST_HAVE # x` to the in-tree allowlist and then
# call this helper legitimately, turning the bounded grant into
# `apt-get install -y <anything>`. Reading only the /etc copy severs THAT path:
# the service has no DIRECT write path to /etc/blackbox (the unit's mount
# namespace makes /etc read-only under ProtectSystem=strict and $ETC_BLACKBOX_DIR
# is barred from ReadWritePaths — perimeter invariant, pinned by
# scripts/tests/test_install_perimeter_invariants.sh), and there is DELIBERATELY
# no service-invokable way to refresh the /etc copy — a new system package is an
# operator step (`sudo bash install.sh`). This is the boundary M2 establishes; it
# is NOT an absolute claim that /etc/blackbox is unreachable. A pre-existing
# residual remains outside M2's scope: the `unit`/`override` target_kinds of
# blackbox-write-systemd copy a caller-supplied source verbatim into a PID-1
# trust anchor, so an RCE could write a drop-in adding ReadWritePaths=/etc/blackbox
# (or User=root) and restart — a MORE direct root path that allowlist-poisoning,
# which M2 neither introduces nor widens (tracked in the plan's M5 trust-anchor
# walk). Do NOT reintroduce a fallback to the repo-relative allowlist; that
# fallback WAS the escalation.
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
#   4 — /etc/blackbox allowlist absent/unreadable (fail closed; names remedy)
#   5 — package not in allowlist

set -euo pipefail

PACKAGE="${1:-}"

# ── Allowlist source ─────────────────────────────────────────────────────────
# HARDCODED to the root-owned /etc copy. BLACKBOX_SYSTEM_PACKAGES is honoured
# ONLY so the update runner's / the bash-test harness can redirect the read into
# a sandbox — sudo's env_reset strips it on a real invocation through the
# NOPASSWD grant (exactly as the retired $BLACKBOX_ROOT override relied on), so
# production can never be pointed at a service-writable allowlist. There is NO
# repo-relative fallback on purpose: reading the in-tree allowlist (which the
# service CAN write) is the escalation this rewrite exists to close.
ALLOWLIST_FILE="${BLACKBOX_SYSTEM_PACKAGES:-/etc/blackbox/system-packages.txt}"

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
    echo "[blackbox-apt-install] ERROR: root-owned allowlist not readable: $ALLOWLIST_FILE" >&2
    # Fail closed. install.sh writes this file (root:root 0644) before its first
    # apt run, so an absent copy means the box has never completed a privileged
    # install — or the file was hand-mangled. The remedy is the one operator
    # step this whole design intends: re-run the installer as root.
    echo "[blackbox-apt-install] (Run 'sudo bash <blackbox-root>/Scripts/install.sh' once to write it)" >&2
    exit 4
fi

# Parse allowlist — same grep pattern install.sh Step 1 uses to install the
# initial set. Format: `<package>  # <bucket> # <reason>`. Buckets MUST_HAVE
# and SHOULD_HAVE both pass. This membership check is now an RCE boundary (not
# merely an argv-injection bound), because the source is root-owned and no
# automated step can rewrite it.
ALLOWED=$(grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' "$ALLOWLIST_FILE" | awk '{print $1}')

# Fixed-string + exact-line match. -F disables regex, -x requires whole line.
if ! echo "$ALLOWED" | grep -qFx "$PACKAGE"; then
    echo "[blackbox-apt-install] ERROR: package not in allowlist: $PACKAGE" >&2
    # The /etc copy is generated — do NOT tell the operator to hand-edit it.
    # Adding a package is a repo edit + installer re-run (the one operator step).
    echo "[blackbox-apt-install] (A new system package is an operator step: add it to" >&2
    echo "[blackbox-apt-install]  Scripts/onboarding/system-packages.txt and run 'sudo bash install.sh')" >&2
    exit 5
fi

echo "[blackbox-apt-install] Installing $PACKAGE (allowlisted)..."
/usr/bin/apt-get install -y --no-install-recommends "$PACKAGE"
echo "[blackbox-apt-install] $PACKAGE installed."
exit 0
