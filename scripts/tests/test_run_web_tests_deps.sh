#!/usr/bin/env bash
# Test for scripts/run_web_tests.sh's dependency install (U37 review).
#
# The script ran `npm ci` only when tests/web/node_modules was missing, so a
# checkout that already had one from before a lockfile change (the dev box's
# held marked 11.1.1 and none of the xterm/noVNC/highlight.js pins) failed
# vendor-pins.test.mjs after a pull until someone reinstalled by hand.
#
# Proves, against a copy of the real script in a mktemp sandbox (PATH stubs
# for npm, node and git; nothing is installed or run):
#   1. a missing node_modules is installed;
#   2. an install whose lockfile has not changed is reused;
#   3. a lockfile change reinstalls;
#   4. a node_modules from before this check (no record of its lockfile)
#      reinstalls once, and is reused after that.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/run_web_tests.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$SCRIPT" ]] || fail "run_web_tests.sh not found at $SCRIPT"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BOX="$WORK/repo"
STUBS="$WORK/stubbin"
LOG="$WORK/calls.log"
mkdir -p "$BOX/scripts" "$BOX/tests/web" "$BOX/Portal" "$STUBS"
cp "$SCRIPT" "$BOX/scripts/run_web_tests.sh"
echo '{"lockfileVersion": 3, "packages": {"node_modules/marked": {"version": "11.1.1"}}}' \
    > "$BOX/tests/web/package-lock.json"
: > "$BOX/tests/web/a.test.mjs"

# npm ci: recreate node_modules from scratch, as the real one does.
cat > "$STUBS/npm" <<'STUB'
#!/usr/bin/env bash
echo "npm $*" >> "$STUB_LOG"
prefix=""
while [[ $# -gt 0 ]]; do
    [[ "$1" == "--prefix" ]] && prefix="$2"
    shift
done
rm -rf "$prefix/node_modules" && mkdir -p "$prefix/node_modules"
STUB
cat > "$STUBS/node" <<'STUB'
#!/usr/bin/env bash
echo "node $*" >> "$STUB_LOG"
STUB
# Not a work tree: the script falls back to find (and never sees a real repo).
printf '#!/usr/bin/env bash\nexit 128\n' > "$STUBS/git"
chmod +x "$STUBS/npm" "$STUBS/node" "$STUBS/git"
export STUB_LOG="$LOG"

run() {  # <case> — runs the script; fails the test when it fails
    : > "$LOG"
    PATH="$STUBS:$PATH" bash "$BOX/scripts/run_web_tests.sh" > "$WORK/$1.log" 2>&1 \
        || fail "$1: run_web_tests.sh failed: $(cat "$WORK/$1.log")"
    grep -q '^node --test .*a\.test\.mjs' "$LOG" || fail "$1: the suites never ran"
}
installed() { grep -q '^npm ci .*--prefix tests/web' "$LOG"; }

# 1. missing
run missing
installed || fail "1: a missing tests/web/node_modules was not installed"

# 2. unchanged lockfile
run unchanged
installed && fail "2: an install of the same lockfile was redone"

# 3. lockfile changed (a pull bumped the pins)
echo '{"lockfileVersion": 3, "packages": {"node_modules/marked": {"version": "18.1.1"}}}' \
    > "$BOX/tests/web/package-lock.json"
run changed
installed || fail "3: a lockfile change did not reinstall node_modules"
run changed-again
installed && fail "3: the reinstalled node_modules was not reused"

# 4. a node_modules installed before this check existed
rm -rf "$BOX/tests/web/node_modules" && mkdir -p "$BOX/tests/web/node_modules/marked"
run legacy
installed || fail "4: a node_modules with no record of its lockfile was reused"
run legacy-again
installed && fail "4: the reinstalled node_modules was not reused"

echo "ALL TESTS PASSED"
exit 0
