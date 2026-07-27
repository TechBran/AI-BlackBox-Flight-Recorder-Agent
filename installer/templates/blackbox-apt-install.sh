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
# Both checks must pass. Be precise about what that does and does not buy
# (corrected 2026-07-27 — the previous wording claimed the allowlist was
# "customer-non-writable", which is false on a live box): the allowlist lives in
# $BLACKBOX_ROOT, and the whole of $BLACKBOX_ROOT is in blackbox.service's
# ReadWritePaths, so the service user CAN append to it. What these two checks
# bound is argv injection through the sudo grant (no metacharacters, no path
# traversal, no arbitrary `apt-get` invocation) and accidental misuse — NOT a
# service-user RCE, which can edit the allowlist (or install.sh, which has its
# own bounded grant) and then call this helper legitimately. Closing that gap
# needs allowlist integrity, not a stricter argv check; see
# docs/runbooks/2026-07-27-stuck-update-stale-helper.md ("Known gap").
#
# Invoked via NOPASSWD sudoers grant:
#   bbx ALL=(root) NOPASSWD: /usr/local/sbin/blackbox-apt-install *
#
# Usage:
#   sudo blackbox-apt-install <package-name>
#
# INSTALL ROOT: see the resolution block below. This file is installed to
# /usr/local/sbin root-owned, so `git reset --hard` refreshes the template in
# the repo but can NEVER touch the installed copy — a root path baked in here
# outlives every update that follows it (customer, 2026-07-27).
#
# Exit codes:
#   0 — installed successfully (or already installed; apt is idempotent)
#   2 — missing package argument
#   3 — package name failed regex check
#   4 — allowlist file unreadable
#   5 — package not in allowlist

set -euo pipefail

PACKAGE="${1:-}"

# ── Install-root resolution ──────────────────────────────────────────────
# Customer, 2026-07-27: this used to be a single hardcoded default — the
# canonical Track 4 path. Their box was installed elsewhere AND their helper
# predated the 5eabbe45 rename, so every apt phase died at exit 4 against a
# directory that had never existed on that machine. Three sources now, most
# specific first, so no single stale constant can strand a box:
#
#   1. $BLACKBOX_ROOT — testing only. sudo's env_reset strips it in prod, so
#      this is never the production path; it is kept because the update
#      runner's own test harness relies on it.
#   2. the root pointer — root-owned 0644, rewritten by install.sh Step 0b
#      on EVERY run. A repo that moves is repaired by re-running install.sh
#      alone; nothing here has to be hand-edited under /usr/local/sbin.
#      It lives under /etc, outside the service's ReadWritePaths, so the
#      service cannot rewrite it — an invariant the unit heredoc in install.sh
#      records explicitly, because the pointer OUTRANKS the templated default.
#   3. BLACKBOX_ROOT_PLACEHOLDER — substituted at install time with the real
#      $BLACKBOX_ROOT install.sh derived from its own location. NOT a guess
#      at where the customer "should" have installed.
BLACKBOX_ROOT_POINTER="${BLACKBOX_ROOT_POINTER:-/etc/blackbox/root}"
BLACKBOX_ROOT="${BLACKBOX_ROOT:-}"

if [[ -z "$BLACKBOX_ROOT" && -f "$BLACKBOX_ROOT_POINTER" && -r "$BLACKBOX_ROOT_POINTER" ]]; then
    # `|| POINTED=""` because set -e would otherwise abort the whole helper on
    # an unreadable-mid-read pointer, exiting with a code the update runner
    # cannot interpret. Any read failure must degrade to the templated default.
    POINTED="$(head -n 1 "$BLACKBOX_ROOT_POINTER" 2>/dev/null \
               | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')" || POINTED=""
    # Validate STRUCTURALLY, not by charset: absolute, no `..` component, and
    # actually a BlackBox tree (its allowlist is readable). A character-class
    # regex was tried first and rejected legal install paths — MEASURED: a root
    # containing a space or a parenthesis made every invocation fall back to the
    # templated default, killing the pointer's entire purpose for exactly the
    # moved-repo case it exists to cover. Proving the target holds the allowlist
    # is the stronger check anyway. The `..` reject keeps a pointer from
    # traversing out of the intended tree. A rejected pointer costs nothing:
    # source 3 below is the same path install.sh baked in.
    if [[ "$POINTED" == /* && "$POINTED" != *..* && -r "$POINTED/Scripts/onboarding/system-packages.txt" ]]; then
        BLACKBOX_ROOT="$POINTED"
    elif [[ -n "$POINTED" ]]; then
        echo "[blackbox-apt-install] WARNING: ignoring unusable root pointer $BLACKBOX_ROOT_POINTER" >&2
        echo "[blackbox-apt-install] ('$POINTED' is not an absolute path to a BlackBox install)" >&2
    fi
fi

BLACKBOX_ROOT="${BLACKBOX_ROOT:-BLACKBOX_ROOT_PLACEHOLDER}"
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
    # Almost always means THIS installed copy is stale and still points at a
    # previous install root (customer, 2026-07-27). Name the one-command fix
    # here too — the update runner's preflight says the same thing, but a
    # customer reading raw stderr over SSH never sees that.
    echo "[blackbox-apt-install] (Re-run 'sudo bash <blackbox-root>/Scripts/install.sh' to repoint this helper)" >&2
    exit 4
fi

# Parse allowlist — same grep pattern install.sh Step 1 uses to install the
# initial set. Format: `<package>  # <bucket> # <reason>`. Buckets MUST_HAVE
# and SHOULD_HAVE both pass.
#
# FOLLOW-UP (allowlist integrity, NOT closed here): this membership check bounds
# argv injection through the sudo grant and accidental misuse — it is NOT an
# RCE boundary. The allowlist lives in $BLACKBOX_ROOT, which is in the unit's
# ReadWritePaths, so the service user can append `evil # MUST_HAVE # x` and then
# call this helper legitimately. Closing that needs the helper to validate
# against a root-owned copy (e.g. under /etc/blackbox) or `git show HEAD:` — a
# separate pass. See docs/runbooks/2026-07-27-stuck-update-stale-helper.md.
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
