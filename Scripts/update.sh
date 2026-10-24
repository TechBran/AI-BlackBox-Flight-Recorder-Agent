#!/usr/bin/env bash
# Scripts/update.sh — manual update entry point (T4).
#
# DESIGN: The Portal's "Install update" button → backend update runner
# (Orchestrator/update/*) is the canonical production path; it streams
# SSE progress, holds a flock mutex, persists state machine, and handles
# rollback atomically.
#
# THIS script is the manual / SSH-rescue entry point — when an operator
# is troubleshooting via shell and wants to bypass the Portal. Same end
# state (git reset --hard origin/main + idempotent install.sh re-run +
# service restart), just orchestrated synchronously with interactive
# confirms instead of an SSE stream.
#
# Usage:
#   ./Scripts/update.sh           # interactive: shows commits, asks Y/N
#   ./Scripts/update.sh --yes     # non-interactive (CI / scripted)
#   ./Scripts/update.sh --yes --stash  # ...and stash local edits to tracked files
#   ./Scripts/update.sh --status  # show "X commits behind", exit; no changes
#
# Local edits to TRACKED files block the update (the reset would destroy
# them; the in-app updater refuses the same way). Interactive runs ask to
# stash them; --yes exits 2 unless --stash is given. A stash is named
# auto-stash-<tag> and its SHA printed. Untracked files never block.
#
# Exit codes:
#   0 — updated successfully (or "already up to date")
#   1 — error during update; venvs and code rolled back via the freezes + tag
#   2 — user declined the update or the stash, or --yes met local edits
#       without --stash
#   3 — preflight failure (not a git repo, network unreachable, etc.)

set -euo pipefail

BLACKBOX_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BLACKBOX_ROOT"

YES_MODE=0
STATUS_ONLY=0
STASH_MODE=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES_MODE=1 ;;
        --stash) STASH_MODE=1 ;;
        --status) STATUS_ONLY=1 ;;
        --help|-h)
            sed -n '/^# Usage:/,/^# Exit codes:/p' "$0"
            exit 0
            ;;
        *)
            echo "[update] ERROR: unknown arg: $arg" >&2
            echo "  Try: $0 --help" >&2
            exit 3
            ;;
    esac
done

# ── Preflight ──
if [[ ! -d .git ]]; then
    echo "[update] ERROR: $BLACKBOX_ROOT is not a git checkout." >&2
    echo "[update] Run Scripts/install.sh first (Step 0a will lazy-init git)." >&2
    exit 3
fi

if ! git remote get-url origin > /dev/null 2>&1; then
    echo "[update] ERROR: no 'origin' remote configured. Run install.sh's Step 0a manually." >&2
    exit 3
fi

# ── Origin self-heal (repo renamed 2026-07-23) ──
# blackbox-poc → ai-blackbox-flight-recorder-agent. GitHub 301-redirects the
# old slug, so this line is DELIVERED over the redirect and then retires the
# dependency on it. Idempotent; never recreate a repo named blackbox-poc —
# that would hijack any box that hasn't run this yet.
if git remote get-url origin | grep -q "TechBran/blackbox-poc"; then
    git remote set-url origin https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git
    echo "[update] origin re-pointed to the renamed repo (ai-blackbox-flight-recorder-agent)."
fi

# ── Fetch latest ──
echo "[update] Fetching latest from $(git remote get-url origin)..."
if ! git fetch origin main 2>&1; then
    echo "[update] ERROR: git fetch failed. Network down or github.com unreachable?" >&2
    exit 3
fi

CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_SHORT=$(git rev-parse --short HEAD)
LATEST_SHA=$(git rev-parse origin/main)
LATEST_SHORT=$(git rev-parse --short origin/main)

# Distinguish "same SHA" / "local ahead" / "local behind" — only the last
# is an actual update opportunity. `rev-list --count A..B` counts commits
# reachable from B but not A; if 0, B is not ahead of A.
COMMITS_BEHIND=$(git rev-list --count "$CURRENT_SHA..$LATEST_SHA")
COMMITS_AHEAD=$(git rev-list --count "$LATEST_SHA..$CURRENT_SHA")

if [[ "$COMMITS_BEHIND" -eq 0 ]]; then
    if [[ "$COMMITS_AHEAD" -eq 0 ]]; then
        echo "[update] Already up to date ($CURRENT_SHORT)."
    else
        echo "[update] Local is AHEAD of origin/main by $COMMITS_AHEAD commit(s)."
        echo "[update] (Unpushed work — nothing to pull. Push first if you want them on origin.)"
    fi
    exit 0
fi

echo "[update] Update available: $CURRENT_SHORT → $LATEST_SHORT ($COMMITS_BEHIND commits behind)"
echo "[update] ──────────────────────────────────────────────────"
git log --oneline "$CURRENT_SHA..$LATEST_SHA"
echo "[update] ──────────────────────────────────────────────────"

# ── Local edits to tracked files (audit M2, O16-01) ──
# The reset below destroys them; the pre-update tag protects committed work
# only. Untracked files (ledger, uploads, Manifest/) survive a reset, so only
# tracked changes count. Decided before anything is tagged or reset.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY" ]]; then
    echo "[update] Local edits to tracked files (the update would overwrite them):"
    printf '%s\n' "$DIRTY"
fi

if [[ "$STATUS_ONLY" -eq 1 ]]; then
    exit 0
fi

if [[ -n "$DIRTY" && "$STASH_MODE" -ne 1 ]]; then
    if [[ "$YES_MODE" -eq 1 ]]; then
        echo "[update] Refusing to update over local edits in --yes mode." >&2
        echo "[update] Commit or stash them yourself, or re-run with --yes --stash." >&2
        exit 2
    fi
    read -p "[update] Stash these edits and continue? (y/N) " -n 1 -r REPLY || REPLY=""
    echo
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "[update] Aborted to preserve local edits. Commit/stash them yourself + re-run."
        exit 2
    fi
    STASH_MODE=1
fi

# ── User confirm ──
if [[ "$YES_MODE" -ne 1 ]]; then
    read -p "[update] Apply this update? (y/N) " -n 1 -r REPLY || REPLY=""
    echo
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "[update] Aborted by user."
        exit 2
    fi
fi

# ── Rollback anchors (audit M1 + audit C2 staging discipline) ──
PRE_TAG="pre-update-$(date +%s)"
git tag "$PRE_TAG"
echo "[update] Tagged $PRE_TAG (rollback anchor)"

mkdir -p Manifest
if [[ -x Orchestrator/venv/bin/pip ]]; then
    Orchestrator/venv/bin/pip freeze > Manifest/pre_update_pip_freeze.txt 2>/dev/null || true
    echo "[update] Saved Manifest/pre_update_pip_freeze.txt for venv rollback"
fi
if [[ -x MCP/venv/bin/pip ]]; then
    MCP/venv/bin/pip freeze > Manifest/pre_update_mcp_freeze.txt 2>/dev/null || true
    echo "[update] Saved Manifest/pre_update_mcp_freeze.txt for MCP venv rollback"
fi

# ── Stash local edits (agreed above) ──
if [[ -n "$DIRTY" ]]; then
    if ! git stash push -m "auto-stash-${PRE_TAG}"; then
        echo "[update] ERROR: git stash failed; nothing was reset." >&2
        exit 1
    fi
    echo "[update] Stashed local edits as auto-stash-${PRE_TAG} ($(git rev-parse -q --verify refs/stash))."
    echo "[update] Recover them with: git stash list && git stash apply <that SHA>"
fi

# Which venvs this update touches: a failed run restores only those (a full
# --force-reinstall from a freeze takes minutes).
CHANGED_FILES="$(git diff --name-only "$CURRENT_SHA" "$LATEST_SHA")"

# ── Rollback: venvs from the pre-update freezes, then code (O22-09) ──
restore_venv() {  # <pip> <freeze file>
    local pip="$1" freeze="$2" restore
    [[ -x "$pip" && -s "$freeze" ]] || return 0
    # Same rule as the in-app runner: a pin with a local version label
    # (torch==2.11.0+cpu) came from another index, PyPI cannot serve it, and
    # one such line makes pip refuse the whole file. Updates never touch them.
    restore="${freeze%.txt}_restore.txt"
    grep -Ev '^[A-Za-z0-9][A-Za-z0-9._-]*==[^[:space:]]*\+' "$freeze" > "$restore" || true
    echo "[update] Restoring the venv behind $pip from $freeze..."
    if ! "$pip" install -r "$restore" --quiet --force-reinstall; then
        echo "[update] WARNING: venv restore failed; retry: $pip install -r $restore --force-reinstall" >&2
    fi
}

rollback_to_pre_tag() {
    if grep -qxE 'requirements(\.lock)?\.txt' <<< "$CHANGED_FILES"; then
        restore_venv Orchestrator/venv/bin/pip Manifest/pre_update_pip_freeze.txt
    fi
    if grep -qx 'MCP/requirements.txt' <<< "$CHANGED_FILES"; then
        restore_venv MCP/venv/bin/pip Manifest/pre_update_mcp_freeze.txt
    fi
    # A reset like any other: stash what the failed run left in tracked files.
    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
        if git stash push -m "auto-stash-rollback-${PRE_TAG}"; then
            echo "[update] Stashed edits left by the failed run as auto-stash-rollback-${PRE_TAG} ($(git rev-parse -q --verify refs/stash))."
        fi
    fi
    git reset --hard "$PRE_TAG"
}

# ── Atomic code swap ──
echo "[update] Resetting to origin/main ($LATEST_SHORT)..."
git reset --hard origin/main

# ── Re-run idempotent install.sh steps + restart ──
# install.sh's existing if-not-exists guards prevent overwriting per-customer
# state (.env, config.ini, devices.json). pip install / npm install / sudo tee
# are idempotent. Helper-script copies overwrite (always-current). Tauri build
# rebuilds if cargo decides it's stale (usually a no-op).
echo "[update] Re-running install.sh to apply system-level changes..."
if ! sudo "$BLACKBOX_ROOT/Scripts/install.sh"; then
    echo "[update] ERROR: install.sh failed. Rolling back venvs and code to $PRE_TAG..." >&2
    rollback_to_pre_tag
    echo "[update] Rolled back. Service was NOT restarted (still at old SHA)." >&2
    echo "[update] Investigate: journalctl -u blackbox.service -n 50 | tail" >&2
    exit 1
fi

# ── Restart service ──
echo "[update] Restarting blackbox.service..."
sudo systemctl restart blackbox.service

echo
echo "[update] ════════════════════════════════════════════════════"
echo "[update] Update complete: $CURRENT_SHORT → $LATEST_SHORT"
echo "[update] Rollback anchor: git tag $PRE_TAG"
echo "[update] To rollback:"
echo "[update]   git reset --hard $PRE_TAG \\"
echo "[update]   && sudo $BLACKBOX_ROOT/Scripts/install.sh \\"
echo "[update]   && sudo systemctl restart blackbox.service"
echo "[update] ════════════════════════════════════════════════════"
exit 0
