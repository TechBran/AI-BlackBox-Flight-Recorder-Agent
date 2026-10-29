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
# Updates come only from the origin the root-owned /etc/blackbox/update-origin
# pins (checked before the fetch). The commit installed is main as that origin
# itself reports it (git ls-remote, outside the checkout), never the checkout's
# own origin/main. Once /etc/blackbox/update-allowed-signers holds a release
# key, only commits SSH-signed by it are installed. Its objects must hash to
# their ids before the reset, and every tracked file on disk must be its blob
# after the reset, before root runs install.sh from the tree. Every git call
# runs without replace refs, hooks, fsmonitor or pager (2026-10 audit
# X04-security-clients-infra-11;
# docs/runbooks/2026-10-29-update-trust-and-service-user.md). A failed
# install.sh puts back the venvs, the code, the stash taken for the update and
# the old code's root-owned template store.
#
# Exit codes:
#   0 — updated successfully (or "already up to date")
#   1 — error during update; venvs and code rolled back via the freezes + tag
#   2 — user declined the update or the stash, or --yes met local edits
#       without --stash
#   3 — preflight failure (not a git repo, network unreachable, etc.)
#   4 — refused by the trust checks: the origin is not a pinned one, the
#       commit is not signed by a pinned release key, or .git does not hold
#       the commit's real content (nothing was changed); or the files on disk
#       after the reset are not the commit's (the code was put back, and
#       install.sh never ran)

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

# ── Who may touch the checkout (2026-10 audit X04-security-clients-infra-12) ──
# On a box moved to a dedicated service account (sudo bash install.sh
# --service-user <name>) the checkout belongs to that account and the operator
# running this script is another, sudo-capable one. Every git, pip and file
# step on the checkout then runs as its owner, so nothing in it ends up owned
# by the operator or by root; only install.sh and the restart run as root. On
# every other box the owner is the caller and as_owner changes nothing.
#
# Every git call on the checkout carries GIT_SAFE (2026-10 audit
# X04-security-clients-infra-11; the in-app updater's tree_check.GIT_SAFE_OPTIONS,
# kept identical by a test): .git belongs to the service, so a refs/replace/
# entry could swap the tree checked out under a verified commit, a hook or an
# fsmonitor program could run during the reset, and a pager during `git log`.
# Command-line options beat anything .git/config says.
GIT_SAFE=(--no-replace-objects --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false)
TREE_OWNER="$(stat -c %U "$BLACKBOX_ROOT")"
if [[ "$TREE_OWNER" != "$(id -un)" ]]; then
    as_owner() { sudo -u "$TREE_OWNER" -- "$@"; }
    GIT_CMD=(sudo -u "$TREE_OWNER" -- git)
    echo "[update] $BLACKBOX_ROOT belongs to $TREE_OWNER: its git and pip steps run as $TREE_OWNER."
else
    as_owner() { "$@"; }
    GIT_CMD=(git)
fi
git() { command "${GIT_CMD[@]}" "${GIT_SAFE[@]}" "$@"; }

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

# ── Pinned origin + release signature (2026-10 audit X04-security-clients-infra-11) ──
# The in-app updater's rules, line for line (Orchestrator/update/verify.py has
# the reference text). The origin must be one of the root-owned
# $UPDATE_TRUST_DIR/update-origin lines, compared ignoring case, one trailing
# "/" and one trailing ".git", and it is checked BEFORE the fetch. Once the
# root-owned $UPDATE_TRUST_DIR/update-allowed-signers holds a key, the commit
# installed must carry an SSH signature that git verify-commit accepts against
# that file alone: the -c options beat .git/config (which the service can
# write), and the PGP and X.509 verifiers are `false`. A pin that is a symlink,
# not owned by root or writable by anyone else refuses the update; a missing
# one only warns (a box that has not run the install.sh that publishes them, or
# a repo that ships no release key yet). Both constants are fixed: nothing in
# the caller's environment can point the check anywhere else.
UPDATE_TRUST_DIR=/etc/blackbox
UPDATE_TRUST_UID=0

refuse_update() {
    echo "[update] REFUSED: $*" >&2
    echo "[update] Nothing was changed." >&2
    exit 4
}

# Loads pin <name> into PIN_LINES (its lines, trimmed, without comments and
# blank lines). Returns 1 when the pin does not exist; refuses the update when
# the pin or the directory holding it cannot be trusted.
load_pin() {  # <name>
    local path="$UPDATE_TRUST_DIR/$1" p line
    PIN_LINES=()
    [[ -e "$path" || -L "$path" ]] || return 1
    for p in "$UPDATE_TRUST_DIR" "$path"; do
        if [[ -L "$p" || "$(stat -c %u "$p")" != "$UPDATE_TRUST_UID" ]] \
                || (( 8#$(stat -c %a "$p") & 8#022 )); then
            refuse_update "$p is not a trusted pin: it must be root-owned, not a symlink, and writable by nobody else (sudo bash $BLACKBOX_ROOT/Scripts/install.sh writes it that way)."
        fi
    done
    [[ -f "$path" ]] || refuse_update "$path is not a regular file, so it is not a trusted pin."
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        PIN_LINES+=("$line")
    done < "$path"
    return 0
}

normalize_origin() {  # <url>: trimmed, lower case, one "/" and one ".git" off the end
    local u="$1"
    u="${u#"${u%%[![:space:]]*}"}"
    u="${u%"${u##*[![:space:]]}"}"
    u="${u,,}"
    u="${u%/}"
    printf '%s\n' "${u%.git}"
}

check_pinned_origin() {
    local url want pin
    ORIGIN_URL="$(git remote get-url origin)"  # where remote_main_sha asks
    if ! load_pin update-origin; then
        echo "[update] WARNING: no update origin is pinned on this box ($UPDATE_TRUST_DIR/update-origin is missing), so the origin was not checked. sudo bash $BLACKBOX_ROOT/Scripts/install.sh pins it." >&2
        return 0
    fi
    [[ ${#PIN_LINES[@]} -gt 0 ]] \
        || refuse_update "$UPDATE_TRUST_DIR/update-origin lists no update origin, so no origin is accepted. Re-run sudo bash $BLACKBOX_ROOT/Scripts/install.sh to restore it."
    url="$ORIGIN_URL"
    want="$(normalize_origin "$url")"
    for pin in "${PIN_LINES[@]}"; do
        [[ "$(normalize_origin "$pin")" == "$want" ]] && return 0
    done
    refuse_update "this checkout fetches updates from $url, which is not a pinned update origin ($UPDATE_TRUST_DIR/update-origin lists ${PIN_LINES[*]}). If the origin was changed by mistake, put it back (as $TREE_OWNER, in $BLACKBOX_ROOT) with: git remote set-url origin ${PIN_LINES[0]}"
}

verify_release_commit() {  # <sha>
    local sha="$1" raw keygen="" no="" p out
    if ! load_pin update-allowed-signers || [[ ${#PIN_LINES[@]} -eq 0 ]]; then
        echo "[update] WARNING: no release signing key is pinned on this box ($UPDATE_TRUST_DIR/update-allowed-signers is missing or holds no key), so the signature of ${sha:0:7} was not checked." >&2
        return 0
    fi
    raw="$(git cat-file commit "$sha")"
    if ! grep -qxF 'gpgsig -----BEGIN SSH SIGNATURE-----' <<< "${raw%%$'\n\n'*}"; then
        refuse_update "commit ${sha:0:7} is not signed with a release key (no SSH signature), and this box only installs signed releases ($UPDATE_TRUST_DIR/update-allowed-signers)."
    fi
    for p in /usr/bin/ssh-keygen /bin/ssh-keygen; do
        if [[ -x "$p" ]]; then keygen="$p"; break; fi
    done
    for p in /usr/bin/false /bin/false; do
        if [[ -x "$p" ]]; then no="$p"; break; fi
    done
    [[ -n "$keygen" && -n "$no" ]] \
        || refuse_update "ssh-keygen (openssh-client) is not installed, so the release signature cannot be checked."
    if ! out="$(git -c gpg.ssh.allowedSignersFile="$UPDATE_TRUST_DIR/update-allowed-signers" \
                    -c gpg.ssh.program="$keygen" -c gpg.program="$no" -c gpg.x509.program="$no" \
                    verify-commit "$sha" 2>&1)"; then
        refuse_update "the signature on commit ${sha:0:7} is not from a pinned release key ($UPDATE_TRUST_DIR/update-allowed-signers): $(tail -n 1 <<< "$out")"
    fi
    echo "[update] Commit ${sha:0:7} is signed by a pinned release key."
}

# The SHA of refs/heads/main at <url>, as that remote itself answers it now:
# the commit this update installs. git ls-remote runs outside the checkout (in
# /, as the caller, without the GIT_* variables that redirect git), so neither
# the checkout's refs (a tag named origin/main wins git's lookup over
# refs/remotes/origin/main; a remote.origin.fetch refspec stops the fetch from
# moving it) nor its config (http.*, insteadOf) can change the answer.
remote_main_sha() {  # <url>
    local out sha ref found=""
    out="$( cd / || exit 1
            for v in "${!GIT_@}"; do [[ "$v" == GIT_TERMINAL_PROMPT ]] || unset "$v"; done
            command git ls-remote -- "$1" refs/heads/main )" || return 1
    while IFS=$'\t' read -r sha ref; do
        if [[ "$ref" == refs/heads/main && "$sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
            found="$sha"
        fi
    done <<< "$out"
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
}

# ── The commit's real content (X04-...-11; Orchestrator/update/tree_check.py) ──
# `objects`: the commit, its trees and its blobs in .git hash to their ids.
# `worktree`: every tracked file on disk is, byte for byte, the commit's blob.
# Hashed by the checker in this process (git, as the checkout's owner, only
# hands it the objects), so a planted pack, a replace ref, a hook or a filter
# cannot pass. $TREE_CHECK is set below, after the origin check.
run_tree_check() {  # <objects|worktree> <sha>: prints the problems, returns non-zero on any
    "$PYTHON3" -I "$TREE_CHECK" "$1" "$BLACKBOX_ROOT" "$2" "${GIT_CMD[@]}"
}

check_pinned_origin

# The checker run_tree_check uses: the copy this script came with, taken before
# the reset replaces the file.
TREE_CHECK="$(mktemp)"
trap 'rm -f "$TREE_CHECK"' EXIT
if [[ ! -f "$BLACKBOX_ROOT/Orchestrator/update/tree_check.py" ]] \
        || ! cat "$BLACKBOX_ROOT/Orchestrator/update/tree_check.py" > "$TREE_CHECK"; then
    echo "[update] ERROR: $BLACKBOX_ROOT/Orchestrator/update/tree_check.py is missing; restore it with: git checkout -- Orchestrator/update/tree_check.py" >&2
    exit 3
fi
PYTHON3=/usr/bin/python3
[[ -x "$PYTHON3" ]] || PYTHON3="$(command -v python3 || true)"
if [[ -z "$PYTHON3" ]]; then
    echo "[update] ERROR: python3 is missing, so the update cannot be checked (sudo apt install python3)." >&2
    exit 3
fi

# ── Fetch latest ──
# Main as the origin reports it is asked first; the fetch then brings that
# commit (or a newer one containing it). The refspec is spelled out, so a
# remote.origin.fetch in .git/config cannot send main elsewhere.
echo "[update] Fetching latest from $ORIGIN_URL..."
if ! LATEST_SHA="$(remote_main_sha "$ORIGIN_URL")"; then
    echo "[update] ERROR: $ORIGIN_URL did not say where its main is. Network down or github.com unreachable?" >&2
    exit 3
fi
if ! git fetch origin "+refs/heads/main:refs/remotes/origin/main" 2>&1; then
    echo "[update] ERROR: git fetch failed. Network down or github.com unreachable?" >&2
    exit 3
fi
if ! git cat-file -e "${LATEST_SHA}^{commit}" 2> /dev/null; then
    echo "[update] ERROR: the fetch did not bring ${LATEST_SHA:0:7} (origin/main moved while fetching?). Run the update again." >&2
    exit 3
fi

CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_SHORT=$(git rev-parse --short HEAD)
LATEST_SHORT="${LATEST_SHA:0:7}"

# Distinguish "same SHA" / "local ahead" / "local behind" — only the last
# is an actual update opportunity. `rev-list --count A..B` counts commits
# reachable from B but not A; if 0, B is not ahead of A.
COMMITS_BEHIND=$(git rev-list --count "$CURRENT_SHA..$LATEST_SHA")
COMMITS_AHEAD=$(git rev-list --count "$LATEST_SHA..$CURRENT_SHA")

if [[ "$COMMITS_BEHIND" -eq 0 ]]; then
    if [[ "$COMMITS_AHEAD" -eq 0 ]]; then
        echo "[update] Already up to date ($CURRENT_SHORT)."
    else
        echo "[update] Local is AHEAD of origin/main ($LATEST_SHORT) by $COMMITS_AHEAD commit(s)."
        echo "[update] (Unpushed work — nothing to pull. Push first if you want them on origin.)"
    fi
    exit 0
fi

echo "[update] Update available: $CURRENT_SHORT → $LATEST_SHORT ($COMMITS_BEHIND commits behind)"
echo "[update] ──────────────────────────────────────────────────"
git log --oneline "$CURRENT_SHA..$LATEST_SHA"
echo "[update] ──────────────────────────────────────────────────"

# The commit the reset below installs: LATEST_SHA itself, never whatever
# origin/main names by then.
verify_release_commit "$LATEST_SHA"
if ! OBJECT_PROBLEMS="$(run_tree_check objects "$LATEST_SHA")"; then
    refuse_update "the objects for commit $LATEST_SHORT in this checkout's .git are not the ones its id names, so it was not installed (if a fetch was interrupted, run the update again):"$'\n'"${OBJECT_PROBLEMS:-the check could not run}"
fi

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
        tmp="$(as_owner mktemp -d "$(git rev-parse --absolute-git-dir)/blackbox-status-XXXXXX")"
        # `env` so the variable reaches git through as_owner's sudo too.
        as_owner env GIT_INDEX_FILE="$tmp/index" git "${GIT_SAFE[@]}" read-tree HEAD \
            && as_owner env GIT_INDEX_FILE="$tmp/index" git "${GIT_SAFE[@]}" --no-optional-locks status --porcelain --untracked-files=no
        rc=$?
        as_owner rm -rf "$tmp"
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
    if frozen="$(as_owner "$1" freeze 2>/dev/null)"; then
        printf '# blackbox-freeze-for: %s\n%s\n' "$PRE_TAG" "$frozen" | as_owner tee "$2" > /dev/null
        echo "[update] Saved $2 for venv rollback"
    else
        as_owner rm -f "$2"
        echo "[update] WARNING: could not freeze the venv behind $1; a rollback keeps its packages." >&2
    fi
}

as_owner mkdir -p Manifest
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
    # Manifest/ is the owner's alone (0700, install.sh), so ask as the owner.
    if [[ ! -x "$pip" ]] || ! as_owner test -s "$freeze"; then
        return 0
    fi
    # Same rule as the in-app runner: a pin with a local version label
    # (torch==2.11.0+cpu) came from another index, PyPI cannot serve it, and
    # one such line makes pip refuse the whole file. Updates never touch them.
    # Comments (the freeze's tag line) and blank lines go too.
    restore="${freeze%.txt}_restore.txt"
    { as_owner grep -Ev '^(#|$)|^[A-Za-z0-9][A-Za-z0-9._-]*==[^[:space:]]*\+' "$freeze" || true; } \
        | as_owner tee "$restore" > /dev/null
    as_owner test -s "$restore" || return 0
    echo "[update] Restoring the venv behind $pip from $freeze..."
    if ! as_owner "$pip" install -r "$restore" --quiet --force-reinstall; then
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
    KEPT_STASH=""  # the stash made here, if any (the rollback puts it back)
    if [[ -n "$(tracked_edits)" ]]; then
        if [[ "$STASH_MODE" -ne 1 ]]; then
            echo "[update] Tracked files changed after the last check:"
            tracked_edits
        fi
        if ! git stash push -q -m "$message"; then
            echo "[update] ERROR: git stash failed, so nothing was reset." >&2
            return 1
        fi
        KEPT_STASH="$(git rev-parse -q --verify refs/stash)"
        echo "[update] Stashed local edits to tracked files as $message ($KEPT_STASH)."
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
        # $aside is under Manifest/, which only the owner can read (0700).
        if as_owner test -e "$aside/$rel" || as_owner test -L "$aside/$rel" \
                || ! as_owner mkdir -p "$aside/$(dirname -- "$rel")" \
                || ! as_owner mv -- "$rel" "$aside/$rel"; then
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
    reset_code_to_pre_tag
}

# The code back to PRE_TAG. A reset like any other: keep what the failed run
# left first (it stays in the stash list), and never reset when that fails.
reset_code_to_pre_tag() {
    if ! STASH_MODE=1 keep_local_work "auto-stash-rollback-${PRE_TAG}" "$PRE_TAG" "rollback-${PRE_TAG}"; then
        echo "[update] The code was NOT rolled back to $PRE_TAG: the new code is still on disk." >&2
        echo "[update] Commit or stash the changed files, then run: git reset --hard $PRE_TAG" >&2
        return 1
    fi
    git reset --hard "$PRE_TAG"
}

# ── After the rollback: the stash and the trusted store go back too (X04-...-11) ──
# The stash taken for this update was made on PRE_TAG, so it applies cleanly
# now that the code is back there; once it has, its entry is dropped (found by
# SHA: an entry's stash@{n} moves whenever another stash is made). What the
# rollback itself stashed (the failed run's leftovers) stays in the list.
put_back_update_stash() {
    local sha ref
    [[ -n "$UPDATE_STASH" ]] || return 0
    if ! git stash apply -q "$UPDATE_STASH"; then
        echo "[update] WARNING: the stashed local edits ($UPDATE_STASH) could not be put back; restore them with: git stash apply $UPDATE_STASH" >&2
        return 0
    fi
    while read -r sha ref; do
        if [[ "$sha" == "$UPDATE_STASH" ]]; then
            git stash drop -q "$ref"
            break
        fi
    done < <(git stash list --format='%H %gd')
    echo "[update] Put the local edits stashed for the update back in the working tree."
}

# The failed install.sh had already published the NEW templates to the
# root-owned store and installed the new /usr/local/sbin helpers (Steps 0b/0c
# run before anything that can fail), and the startup hooks re-assert that store
# onto the deployed sudoers and helpers. Republish the old code's. An install.sh
# from before --publish-store-only (no such arm in its option parser) would run
# the whole installer instead, so it is only named.
republish_trusted_store() {
    if ! grep -qE '^[[:space:]]*--publish-store-only\)' "$BLACKBOX_ROOT/Scripts/install.sh"; then
        echo "[update] The root-owned template store may still hold the failed update's templates." >&2
        echo "[update] Republish this commit's with: sudo bash $BLACKBOX_ROOT/Scripts/install.sh" >&2
        return 0
    fi
    echo "[update] Republishing the root-owned template store from $PRE_TAG..."
    if ! sudo "$BLACKBOX_ROOT/Scripts/install.sh" --publish-store-only; then
        echo "[update] WARNING: republishing the template store failed; run: sudo bash $BLACKBOX_ROOT/Scripts/install.sh --publish-store-only" >&2
    fi
}

# ── Atomic code swap ──
# To the commit verify_release_commit checked, by SHA (not origin/main, which
# a hostile .git or a concurrent fetch can move meanwhile).
if ! keep_local_work "auto-stash-${PRE_TAG}" "$LATEST_SHA" "$PRE_TAG"; then
    exit 1
fi
UPDATE_STASH="$KEPT_STASH"
echo "[update] Resetting to $LATEST_SHORT (origin/main as checked above)..."
git reset --hard "$LATEST_SHA"

# ── What root is about to run from: the verified commit, byte for byte ──
# Checked after the reset and before install.sh. Nothing has run from the new
# code yet: the venvs are untouched and the template store still holds the old
# code's, so a mismatch puts back the code and the stash only.
if ! TREE_PROBLEMS="$(run_tree_check worktree "$LATEST_SHA")"; then
    echo "[update] REFUSED: after the reset these tracked files are not the ones commit $LATEST_SHORT holds, so install.sh was not run:" >&2
    printf '%s\n' "${TREE_PROBLEMS:-the check could not run}" >&2
    if reset_code_to_pre_tag; then
        put_back_update_stash
        echo "[update] The code is back at $PRE_TAG (the files found are kept in the stash list). Investigate what rewrote them before updating again." >&2
    else
        echo "[update] Do not run install.sh or restart the service until the code is back at $PRE_TAG." >&2
    fi
    exit 4
fi

# ── Re-run idempotent install.sh steps + restart ──
# install.sh's existing if-not-exists guards prevent overwriting per-customer
# state (.env, config.ini, devices.json). pip install / npm install / sudo tee
# are idempotent. Helper-script copies overwrite (always-current). Tauri build
# rebuilds if cargo decides it's stale (usually a no-op).
echo "[update] Re-running install.sh to apply system-level changes..."
if ! sudo "$BLACKBOX_ROOT/Scripts/install.sh"; then
    echo "[update] ERROR: install.sh failed. Rolling back venvs and code to $PRE_TAG..." >&2
    if rollback_to_pre_tag; then
        put_back_update_stash
        republish_trusted_store
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
