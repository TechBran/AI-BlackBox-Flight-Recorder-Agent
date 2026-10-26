#!/usr/bin/env bash
# Run every Portal node test suite (audit rank 34 / NEW-portal-web-1): the
# Portal's own Portal/**/*.test.mjs, run in place (Portal/package.json marks
# its .js files as the ES modules they are), and the jsdom harness in
# tests/web/*.test.mjs. The CI web job runs exactly this.
#
# tests/web/node_modules (jsdom and the vendored Portal libraries' npm
# releases; pinned in its package-lock.json) is installed with
# `npm ci --ignore-scripts` when missing or when package-lock.json changed
# since the last install. It lives outside Portal/ on purpose: Portal/ is
# served to browsers at /ui, so a node_modules there would be too.
#
#   bash scripts/run_web_tests.sh
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# A node_modules from an older lockfile fails vendor-pins.test.mjs (it checks
# the installed versions), so reinstall when the lockfile changed, not only
# when node_modules is missing. npm's own node_modules/.package-lock.json is
# not a byte copy of package-lock.json, so each install records the sha256 of
# the lockfile it installed.
lock_sum="$(sha256sum tests/web/package-lock.json | cut -d' ' -f1)"
lock_stamp=tests/web/node_modules/.run_web_tests.lock.sha256
if [ ! -d tests/web/node_modules ] || [ "$(cat "$lock_stamp" 2>/dev/null || true)" != "$lock_sum" ]; then
    npm ci --ignore-scripts --no-audit --no-fund --prefix tests/web
    printf '%s\n' "$lock_sum" > "$lock_stamp"
fi

# The tracked suites only: a stray local copy or an editor backup never runs.
# (In a git pathspec `*` also matches `/`, so this covers every subdirectory.)
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    mapfile -t suites < <(git ls-files -- 'Portal/*.test.mjs' 'tests/web/*.test.mjs')
else
    mapfile -t suites < <(find Portal tests/web -name '*.test.mjs' \
        -not -path '*/node_modules/*' | sort)
fi
if [ "${#suites[@]}" -eq 0 ]; then
    echo "no node test suites found" >&2
    exit 1
fi

echo "running ${#suites[@]} node test suites"
node --test "${suites[@]}"
