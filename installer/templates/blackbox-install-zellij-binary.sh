#!/usr/bin/env bash
# blackbox-install-zellij-binary — root-owned dispatch helper for installing
# the pinned Zellij binary into /usr/local/bin/zellij and bouncing the
# zellij-web.service daemon.
#
# WHY: The orchestrator namespace runs with ProtectSystem=strict (see MEMORY:
# protectsystem_strict_blast_radius) so it cannot write /usr/local/bin/ even
# via sudo from inside the service. The update pipeline (Track F) and the
# onboarding "Retry Zellij install" remediation (Track E I6) need a bounded
# root operation that does ONLY: download → verify → atomic install → restart.
#
# Granting NOPASSWD sudo for raw `curl | tar | mv` would let any
# prompt-injection RCE through any MCP tool drop an arbitrary binary into
# /usr/local/bin/zellij = full pwn on next zellij-web restart. Instead this
# helper is the ONLY thing sudo lets through, and it validates the requested
# version against the pinned `zellij-version` template + verifies the
# downloaded artifact's sha256 before any move.
#
# Invoked via NOPASSWD sudoers grant:
#   bbx ALL=(root) NOPASSWD: /usr/local/sbin/blackbox-install-zellij-binary *
#
# Usage:
#   sudo blackbox-install-zellij-binary <version>
#
# The version arg MUST match the single non-comment line in
# $BLACKBOX_ROOT/installer/templates/zellij-version. Any mismatch is rejected
# with exit 3 — an attacker cannot pass `1.0.0-malicious` and have it install.
# Downgrades and upgrades both work, as long as the target equals the pinned
# value (the update pipeline bumps the file BEFORE invoking this helper).
#
# Architecture: x86_64-unknown-linux-musl only for v1 customer hardware.
# aarch64 + others are "address later" per plan.
#
# Phase 0 finding #4 (critical sha256 trap): the GitHub-published .sha256sum
# file hashes the EXTRACTED binary at path
# `target/x86_64-unknown-linux-musl/release/zellij` — NOT the .tar.gz.
# Verifying the tarball will ALWAYS fail. This helper extracts first, then
# verifies the extracted binary against the published hash.
#
# Exit codes:
#   0 — installed (or already-installed, idempotent skip) + daemon restarted
#   2 — missing version argument
#   3 — requested version does not match pinned zellij-version file
#   4 — download failed (network, 404, etc.)
#   5 — sha256 mismatch (the catch-an-attacker exit)
#   6 — systemctl restart zellij-web.service failed (or unit not active post-restart)
#   7 — lock held: another install in progress (concurrent invocation rejected)

set -euo pipefail

VERSION="${1:-}"
# BLACKBOX_ROOT defaults to the canonical Track 4 customer install path.
# sudo strips env by default — the default is what gets used in prod.
BLACKBOX_ROOT="${BLACKBOX_ROOT:-/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main}"
PINNED_FILE="${BLACKBOX_ROOT}/installer/templates/zellij-version"
DEST="/usr/local/bin/zellij"

if [[ -z "$VERSION" ]]; then
    echo "[blackbox-install-zellij-binary] ERROR: no version specified" >&2
    echo "Usage: blackbox-install-zellij-binary <version>" >&2
    exit 2
fi

# Read pinned version — single non-comment, non-blank line in the template.
if [[ ! -r "$PINNED_FILE" ]]; then
    echo "[blackbox-install-zellij-binary] ERROR: pinned-version file not readable: $PINNED_FILE" >&2
    exit 3
fi

PINNED=$(grep -vE '^[[:space:]]*(#|$)' "$PINNED_FILE" | head -n 1 | tr -d '[:space:]')

if [[ -z "$PINNED" ]]; then
    echo "[blackbox-install-zellij-binary] ERROR: pinned-version file has no version line: $PINNED_FILE" >&2
    exit 3
fi

if [[ "$VERSION" != "$PINNED" ]]; then
    echo "[blackbox-install-zellij-binary] ERROR: requested version '$VERSION' does not match pinned '$PINNED'" >&2
    echo "[blackbox-install-zellij-binary] (Bump $PINNED_FILE first, then re-invoke with the matching version)" >&2
    exit 3
fi

# Concurrency lock — Track F update timer can collide with Track E onboarding
# "Retry Zellij install" button. Without serialization, two simultaneous
# processes race on $DEST.install-tmp (second `mv` lands mid-first `cp` =
# truncated /usr/local/bin/zellij) and double `systemctl restart` flaps the
# daemon. -n = non-blocking: fail-fast with exit 7 rather than queue forever
# (the second invoker is doing the same work anyway, no value in waiting).
# fd 9 is held for the install+restart sequence; auto-released on script exit.
# Cheap arg/pinned-file checks above are concurrent-safe and intentionally
# outside the lock.
LOCK_FILE="/var/lock/blackbox-install-zellij-binary.lock"
exec 9>"$LOCK_FILE"
if ! /usr/bin/flock -n 9; then
    echo "[blackbox-install-zellij-binary] ERROR: another install already in progress (lock $LOCK_FILE held)" >&2
    echo "[blackbox-install-zellij-binary] (Wait for the in-flight install to complete, or check 'fuser $LOCK_FILE' for the holder PID)" >&2
    exit 7
fi

# Idempotency: if /usr/local/bin/zellij already reports the requested version,
# skip download. Update pipeline can re-run safely; "Retry Zellij install"
# button in onboarding is no-op after success. Still bounce the daemon at the
# end so a hung session recovers.
SKIP_INSTALL=0
if [[ -x "$DEST" ]]; then
    # `zellij --version` prints e.g. "zellij 0.44.3"
    CURRENT=$("$DEST" --version 2>/dev/null | awk '{print $2}' || true)
    if [[ "$CURRENT" == "$VERSION" ]]; then
        echo "[blackbox-install-zellij-binary] Already at version $VERSION, skipping download"
        SKIP_INSTALL=1
    fi
fi

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    # Temp dir for download + extraction. trap ensures cleanup on ALL exit
    # paths (success or failure) — no orphaned /tmp/zellij-install-* dirs.
    TMPDIR=$(mktemp -d /tmp/zellij-install-XXXXXX)
    trap 'rm -rf "$TMPDIR"' EXIT

    TARBALL_URL="https://github.com/zellij-org/zellij/releases/download/v${VERSION}/zellij-x86_64-unknown-linux-musl.tar.gz"
    SHA_URL="https://github.com/zellij-org/zellij/releases/download/v${VERSION}/zellij-x86_64-unknown-linux-musl.sha256sum"

    TARBALL="${TMPDIR}/zellij.tar.gz"
    SHAFILE="${TMPDIR}/zellij.sha256sum"

    echo "[blackbox-install-zellij-binary] Downloading zellij v${VERSION} (x86_64-linux-musl)..."

    # --fail: non-2xx → curl exits non-zero (catches 404 on a mistyped version)
    # --location: follow GitHub release redirects
    # --silent --show-error: quiet but surface errors
    # NO curl|bash: download to file, install separately. Hard constraint #8.
    if ! /usr/bin/curl --fail --location --silent --show-error \
            --output "$TARBALL" "$TARBALL_URL"; then
        echo "[blackbox-install-zellij-binary] ERROR: tarball download failed: $TARBALL_URL" >&2
        exit 4
    fi

    if ! /usr/bin/curl --fail --location --silent --show-error \
            --output "$SHAFILE" "$SHA_URL"; then
        echo "[blackbox-install-zellij-binary] ERROR: sha256sum download failed: $SHA_URL" >&2
        exit 4
    fi

    # Extract FIRST — Phase 0 finding #4: the published .sha256sum hashes the
    # extracted binary (target/x86_64-unknown-linux-musl/release/zellij path),
    # NOT the tarball. Verifying the tarball would always fail.
    echo "[blackbox-install-zellij-binary] Extracting tarball..."
    if ! /usr/bin/tar -xzf "$TARBALL" -C "$TMPDIR"; then
        echo "[blackbox-install-zellij-binary] ERROR: tarball extraction failed" >&2
        exit 4
    fi

    EXTRACTED="${TMPDIR}/zellij"
    if [[ ! -f "$EXTRACTED" ]]; then
        echo "[blackbox-install-zellij-binary] ERROR: expected zellij binary not found at $EXTRACTED after extract" >&2
        exit 4
    fi

    # Verify EXTRACTED binary against published hash.
    EXPECTED=$(awk '{print $1}' "$SHAFILE")
    ACTUAL=$(/usr/bin/sha256sum "$EXTRACTED" | awk '{print $1}')

    if [[ -z "$EXPECTED" ]]; then
        echo "[blackbox-install-zellij-binary] ERROR: could not parse expected hash from $SHAFILE" >&2
        exit 5
    fi

    if [[ "$EXPECTED" != "$ACTUAL" ]]; then
        echo "[blackbox-install-zellij-binary] ERROR: sha256 mismatch" >&2
        echo "[blackbox-install-zellij-binary]   expected: $EXPECTED" >&2
        echo "[blackbox-install-zellij-binary]   actual:   $ACTUAL" >&2
        exit 5
    fi

    echo "[blackbox-install-zellij-binary] sha256 verified ($ACTUAL)"

    # Atomic install: temp dir is /tmp (tmpfs), /usr/local/bin is on root fs,
    # so a direct mv would cross filesystems and NOT be atomic. Stage the
    # verified binary onto the same filesystem as DEST first, then mv.
    chmod 0755 "$EXTRACTED"
    chown root:root "$EXTRACTED"

    STAGED="${DEST}.install-tmp"
    cp "$EXTRACTED" "$STAGED"
    chmod 0755 "$STAGED"
    chown root:root "$STAGED"

    mv "$STAGED" "$DEST"
    echo "[blackbox-install-zellij-binary] Installed $DEST (version $VERSION)"
fi

# Bounce the daemon so the new binary is picked up. Also recovers a hung
# session in the idempotent-skip case (Retry button after a previous good
# install can still want a fresh daemon).
if ! /usr/bin/systemctl restart zellij-web.service; then
    echo "[blackbox-install-zellij-binary] ERROR: systemctl restart zellij-web.service failed" >&2
    echo "[blackbox-install-zellij-binary] (Check 'journalctl -u zellij-web.service' for details)" >&2
    exit 6
fi

# Verify the unit actually reached `active` — systemctl restart returns 0 once
# the start-job is QUEUED, not when the unit transitions to active. A segfault
# at launch would otherwise report success. Brief settle delay because the
# start-job → active transition is async; without it, is-active can report
# "activating" instead of "active" on a healthy unit.
sleep 1
if ! /usr/bin/systemctl is-active --quiet zellij-web.service; then
    echo "[blackbox-install-zellij-binary] ERROR: zellij-web.service is not active after restart" >&2
    echo "[blackbox-install-zellij-binary] (Check 'journalctl -u zellij-web.service' for details)" >&2
    exit 6
fi

echo "[blackbox-install-zellij-binary] zellij-web.service restarted OK"
exit 0
