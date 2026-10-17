#!/usr/bin/env bash
# Run every Portal node test suite (audit rank 34 / NEW-portal-web-1): the
# Portal's own Portal/**/*.test.mjs, run in place (Portal/package.json marks
# its .js files as the ES modules they are), and the jsdom harness in
# tests/web/*.test.mjs. The CI web job runs exactly this.
#
# tests/web/node_modules (jsdom, dompurify, marked; pinned in its
# package-lock.json) is installed with `npm ci --ignore-scripts` when missing.
# It lives outside Portal/ on purpose: Portal/ is served to browsers at /ui, so
# a node_modules there would be too.
#
#   bash scripts/run_web_tests.sh
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

if [ ! -d tests/web/node_modules ]; then
    npm ci --ignore-scripts --no-audit --no-fund --prefix tests/web
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
