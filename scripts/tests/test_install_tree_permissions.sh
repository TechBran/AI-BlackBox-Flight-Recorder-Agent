#!/usr/bin/env bash
# install.sh's harden_tree_permissions() (2026-10 audit rank 79,
# X04-security-clients-infra-13) on a throwaway checkout.
#
# The service imports from its working directory (the checkout) first, so a
# directory others can write in it is a module-planting hole; Manifest/ and
# credentials/ hold the tokens and credentials. This proves, on a sandbox tree
# shaped like the dev box's (0777 root, Orchestrator/, .claude/commands/,
# Manifest/; 0775 Portal/uploads; a 0666 module):
#   1. every directory loses group and other write, every file other write,
#      and group write on a FILE is left alone;
#   2. Manifest/ and credentials/ end 0700 (credentials/ is created);
#   3. a symlink in the tree is not followed: its target keeps its mode;
#   4. a second run changes nothing and says so;
#   5. the function runs every find/chmod as the checkout's owner (never as
#      root) and is invoked before Step 1's apt run, like the other Step 0
#      blocks, so a later failure cannot skip it.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'chmod -R u+rwx "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
sed -n '/^harden_tree_permissions()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
grep -q 'harden_tree_permissions()' "$FUNC_FILE" || fail "could not extract harden_tree_permissions() from install.sh"
# shellcheck source=/dev/null
source "$FUNC_FILE"

BLACKBOX_ROOT="$WORK/blackbox"
OUTSIDE="$WORK/elsewhere"
mkdir -p "$BLACKBOX_ROOT"/{Orchestrator/routes,Orchestrator/venv/lib,Portal/uploads,.claude/commands,Manifest} "$OUTSIDE"
echo 'x = 1' > "$BLACKBOX_ROOT/Orchestrator/routes/mod.py"
echo 'y = 2' > "$BLACKBOX_ROOT/Orchestrator/routes/shared.py"
chmod 0777 "$BLACKBOX_ROOT" "$BLACKBOX_ROOT/Orchestrator" "$BLACKBOX_ROOT/.claude/commands" \
           "$BLACKBOX_ROOT/Manifest" "$BLACKBOX_ROOT/Orchestrator/venv/lib" "$OUTSIDE"
chmod 0775 "$BLACKBOX_ROOT/Portal/uploads"
chmod 0666 "$BLACKBOX_ROOT/Orchestrator/routes/mod.py"
chmod 0664 "$BLACKBOX_ROOT/Orchestrator/routes/shared.py"
ln -s "$OUTSIDE" "$BLACKBOX_ROOT/Portal/linked"

# As install.sh runs it: a plain statement under set -euo pipefail (a call used
# as a condition, or inside $( ), would run with errexit off).
harden() {
    ( set -euo pipefail; harden_tree_permissions ) > "$WORK/harden.out" 2>&1
    RC=$?
    OUT="$(cat "$WORK/harden.out")"
}
harden
[[ "$RC" -eq 0 ]] || fail "harden_tree_permissions failed ($RC): $OUT"

# ── 1. no group/other write on any directory; no other write on any file ──────
BAD="$(find "$BLACKBOX_ROOT" -type d -perm /022 -print)"
[[ -z "$BAD" ]] || fail "directories still group/other-writable: $BAD"
BAD="$(find "$BLACKBOX_ROOT" -type f -perm -002 -print)"
[[ -z "$BAD" ]] || fail "files still world-writable: $BAD"
[[ "$(stat -c %a "$BLACKBOX_ROOT/Orchestrator/routes/mod.py")" == 664 ]] \
    || fail "a 0666 file should end 0664, got $(stat -c %a "$BLACKBOX_ROOT/Orchestrator/routes/mod.py")"
[[ "$(stat -c %a "$BLACKBOX_ROOT/Orchestrator/routes/shared.py")" == 664 ]] \
    || fail "group write on a file must be left alone"
[[ "$(stat -c %a "$BLACKBOX_ROOT")" == 755 ]] || fail "the root should end 0755, got $(stat -c %a "$BLACKBOX_ROOT")"

# ── 2. Manifest/ and credentials/ are 0700 ───────────────────────────────────
for d in Manifest credentials; do
    [[ -d "$BLACKBOX_ROOT/$d" ]] || fail "$d/ does not exist"
    [[ "$(stat -c %a "$BLACKBOX_ROOT/$d")" == 700 ]] || fail "$d/ is $(stat -c %a "$BLACKBOX_ROOT/$d"), expected 700"
done

# ── 3. symlinks are not followed ─────────────────────────────────────────────
[[ "$(stat -c %a "$OUTSIDE")" == 777 ]] || fail "the symlink's target outside the checkout was changed"

# ── 4. idempotent ────────────────────────────────────────────────────────────
grep -q 'from [1-9][0-9]* directories and other write from 1 files' <<< "$OUT" \
    || fail "the first run does not report what it changed: $OUT"
FIRST="$OUT"
harden
[[ "$RC" -eq 0 ]] || fail "second run failed ($RC): $OUT"
grep -q 'from 0 directories and other write from 0 files' <<< "$OUT" \
    || fail "a second run changed something: $OUT"
# A private directory it cannot fix is reported, never fatal (errexit is on).
rmdir "$BLACKBOX_ROOT/credentials"
ln -s "$WORK/nowhere" "$BLACKBOX_ROOT/credentials"
harden
[[ "$RC" -eq 0 ]] || fail "an unfixable credentials/ aborted the install ($RC): $OUT"
grep -q "WARN: could not make $BLACKBOX_ROOT/credentials 0700" <<< "$OUT" || fail "an unfixable credentials/ is not reported: $OUT"
rm "$BLACKBOX_ROOT/credentials"
OUT="$FIRST"

# ── 5. as the owner, never root; before Step 1 ───────────────────────────────
grep -qE '^[[:space:]]*sudo (find|chmod|mkdir)' "$FUNC_FILE" && fail "harden_tree_permissions runs find/chmod as root"
for cmd in find mkdir chmod; do
    # (find's own `-exec chmod` runs as whoever runs find.)
    n_all="$(grep -E "(^|[[:space:]])$cmd " "$FUNC_FILE" | grep -cv -- "-exec $cmd")"
    n_owner="$(grep -cE "\\$\\{as_owner\\[@\\]\\+\"\\\$\\{as_owner\\[@\\]\\}\"\\} $cmd " "$FUNC_FILE")"
    [[ "$n_all" -ge 1 && "$n_all" -eq "$n_owner" ]] \
        || fail "every $cmd in harden_tree_permissions must run through as_owner ($n_owner of $n_all do)"
done
CALL_AT="$(grep -n '^harden_tree_permissions$' "$INSTALL_SH" | head -n1 | cut -d: -f1)"
APT_AT="$(grep -nE '^[[:space:]]*\| xargs sudo apt install -y' "$INSTALL_SH" | head -n1 | cut -d: -f1)"
[[ -n "$CALL_AT" && -n "$APT_AT" && "$CALL_AT" -lt "$APT_AT" ]] \
    || fail "harden_tree_permissions (line ${CALL_AT:-never}) must run before Step 1's apt (line ${APT_AT:-?})"

echo "ALL TESTS PASSED"
exit 0
