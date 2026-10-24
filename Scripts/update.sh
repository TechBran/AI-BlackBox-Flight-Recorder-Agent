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
# stash them, again after the "Apply" prompt if files changed meanwhile;
# --yes exits 2 unless --stash is given. A stash is named auto-stash-<tag>
# and its SHA printed. Untracked files never block, but one where the update
# adds a file is moved under Manifest/displaced/<tag>/ before the reset (which
# would overwrite it). Edits saved during the venv freezes, after the last
# question, are stashed rather than reset over.
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
# tracked changes count (keep_local_work moves aside the few untracked files
# a reset would overwrite). Decided before anything is tagged or reset.
tracked_edits() {
    local tmp rc
    # A ZIP-installed box (install.sh Step 0a): HEAD names a commit but the
    # index was never filled, so plain status calls every file a staged
    # deletion. Compare the files on disk with HEAD through a throwaway index
    # (as git_ops.tracked_changes does); the box's own index is never written.
    if [[ -z "$(git ls-files -z | head -c 1)" && -n "$(git ls-tree --name-only HEAD 2>/dev/null | head -c 1)" ]]; then
        tmp="$(mktemp -d "$(git rev-parse --absolute-git-dir)/blackbox-status-XXXXXX")"
        GIT_INDEX_FILE="$tmp/index" git read-tree HEAD \
            && GIT_INDEX_FILE="$tmp/index" git --no-optional-locks status --porcelain --untracked-files=no
        rc=$?
        rm -rf "$tmp"
        return "$rc"
    fi
    git status --porcelain --untracked-files=no
}

# --yes refuses local edits without --stash; an interactive run asks.
refuse_or_ask_to_stash() {
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
}

DIRTY="$(tracked_edits)"
if [[ -n "$DIRTY" ]]; then
    echo "[update] Local edits to tracked files (the update would overwrite them):"
    printf '%s\n' "$DIRTY"
fi

if [[ "$STATUS_ONLY" -eq 1 ]]; then
    exit 0
fi

if [[ -n "$DIRTY" && "$STASH_MODE" -ne 1 ]]; then
    refuse_or_ask_to_stash
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

# ── Look again: the operator may have read the changelog for minutes ──
# An edit saved while the prompt was open was reset over unseen. Nothing has
# been tagged or frozen yet, so declining still changes nothing.
DIRTY="$(tracked_edits)"
if [[ -n "$DIRTY" && "$STASH_MODE" -ne 1 ]]; then
    echo "[update] These tracked files changed after the check above (the update would overwrite them):"
    printf '%s\n' "$DIRTY"
    refuse_or_ask_to_stash
fi

# ── Rollback anchors (audit M1 + audit C2 staging discipline) ──
PRE_TAG="pre-update-$(date +%s)"
git tag "$PRE_TAG"
echo "[update] Tagged $PRE_TAG (rollback anchor)"

# Each freeze's first line names this run's tag: the in-app Rollback
# (Orchestrator/update/runner.py, FREEZE_HEADER) reinstalls a freeze only for
# the tag it rolls back to, and this script writes the same files without its
# update_state.json. A failed freeze leaves no file, never an older one.
freeze_venv() {  # <pip> <freeze file>
    local frozen
    if frozen="$("$1" freeze 2>/dev/null)"; then
        printf '# blackbox-freeze-for: %s\n%s\n' "$PRE_TAG" "$frozen" > "$2"
        echo "[update] Saved $2 for venv rollback"
    else
        rm -f "$2"
        echo "[update] WARNING: could not freeze the venv behind $1; a rollback keeps its packages." >&2
    fi
}

mkdir -p Manifest
if [[ -x Orchestrator/venv/bin/pip ]]; then
    freeze_venv Orchestrator/venv/bin/pip Manifest/pre_update_pip_freeze.txt
fi
if [[ -x MCP/venv/bin/pip ]]; then
    freeze_venv MCP/venv/bin/pip Manifest/pre_update_mcp_freeze.txt
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
    # Comments (the freeze's tag line) and blank lines go too.
    restore="${freeze%.txt}_restore.txt"
    grep -Ev '^(#|$)|^[A-Za-z0-9][A-Za-z0-9._-]*==[^[:space:]]*\+' "$freeze" > "$restore" || true
    [[ -s "$restore" ]] || return 0
    echo "[update] Restoring the venv behind $pip from $freeze..."
    if ! "$pip" install -r "$restore" --quiet --force-reinstall; then
        echo "[update] WARNING: venv restore failed; retry: $pip install -r $restore --force-reinstall" >&2
    fi
}

# ── Keep what a reset would destroy (O16-01; the in-app runner's _keep_then_reset) ──
# Stash local edits to tracked files as <message>: the ones agreed to above,
# and any saved since the last question (during the freezes), which are
# stashed rather than reset over. Then move aside untracked files where
# <target> has a file, or needs a directory: `git reset --hard` overwrites
# them without a word and no stash or tag holds them. Returns non-zero, having
# reset nothing, when either step fails.
keep_local_work() {  # <stash message> <reset target> <displaced dir name>
    local message="$1" target="$2" aside="Manifest/displaced/$3" rel part seg
    local -a in_way=() segs=()
    if [[ -n "$(tracked_edits)" ]]; then
        if [[ "$STASH_MODE" -ne 1 ]]; then
            echo "[update] Tracked files changed after the last check:"
            tracked_edits
        fi
        if ! git stash push -q -m "$message"; then
            echo "[update] ERROR: git stash failed, so nothing was reset." >&2
            return 1
        fi
        echo "[update] Stashed local edits to tracked files as $message ($(git rev-parse -q --verify refs/stash))."
        echo "[update] Recover them with: git stash list && git stash apply <that SHA>"
    fi
    while IFS= read -r -d '' rel; do
        # A file or symlink where the target needs a directory, checked from
        # the top (git replaces a symlink there, it does not follow it)...
        IFS=/ read -r -a segs <<< "$rel"
        part=""
        for seg in "${segs[@]:0:${#segs[@]}-1}"; do
            part="${part:+$part/}$seg"
            if [[ -L "$part" || ( -e "$part" && ! -d "$part" ) ]]; then
                in_way+=("$part")
                continue 2
            fi
        done
        # ...or the file itself.
        if [[ -e "$rel" || -L "$rel" ]]; then
            in_way+=("$rel")
        fi
    done < <(git diff --name-only -z --no-renames --diff-filter=A HEAD "$target")
    for rel in ${in_way[@]+"${in_way[@]}"}; do
        [[ -e "$rel" || -L "$rel" ]] || continue  # already moved (a shared parent)
        if [[ -e "$aside/$rel" || -L "$aside/$rel" ]] \
                || ! mkdir -p "$aside/$(dirname -- "$rel")" || ! mv -- "$rel" "$aside/$rel"; then
            echo "[update] ERROR: could not move $rel aside to $aside/, so nothing was reset." >&2
            return 1
        fi
        echo "[update] Moved untracked $rel (the update has a file there) to $aside/$rel."
    done
}

rollback_to_pre_tag() {
    if grep -qxE 'requirements(\.lock)?\.txt' <<< "$CHANGED_FILES"; then
        restore_venv Orchestrator/venv/bin/pip Manifest/pre_update_pip_freeze.txt
    fi
    if grep -qx 'MCP/requirements.txt' <<< "$CHANGED_FILES"; then
        restore_venv MCP/venv/bin/pip Manifest/pre_update_mcp_freeze.txt
    fi
    # A reset like any other: keep what the failed run left first, and never
    # reset when that fails.
    if ! STASH_MODE=1 keep_local_work "auto-stash-rollback-${PRE_TAG}" "$PRE_TAG" "rollback-${PRE_TAG}"; then
        echo "[update] The code was NOT rolled back to $PRE_TAG: the new code is still on disk." >&2
        echo "[update] Commit or stash the changed files, then run: git reset --hard $PRE_TAG" >&2
        return 1
    fi
    git reset --hard "$PRE_TAG"
}

# ── Atomic code swap ──
if ! keep_local_work "auto-stash-${PRE_TAG}" origin/main "$PRE_TAG"; then
    exit 1
fi
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
    if rollback_to_pre_tag; then
        echo "[update] Rolled back. Service was NOT restarted (still at old SHA)." >&2
    else
        echo "[update] Service was NOT restarted. Do not restart it until the code is back at $PRE_TAG." >&2
    fi
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
