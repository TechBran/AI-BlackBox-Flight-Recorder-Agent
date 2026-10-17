#!/usr/bin/env bash
# Run every installer / perimeter shell suite in this directory
# (scripts/tests/test_*.sh) and fail if any of them fails (audit
# O22-scripts-eval-diagnostics-05). The CI shell job runs exactly this; a new
# suite is picked up by its name, with no list to update.
#
# Each suite sandboxes itself in mktemp dirs and never touches /etc, /usr or
# apt. They need bash, coreutils and /usr/sbin/visudo (the sudo package).
#
#   bash scripts/tests/run_all.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shopt -s nullglob
suites=("$here"/test_*.sh)
if [ "${#suites[@]}" -eq 0 ]; then
    echo "no shell suites found in $here" >&2
    exit 1
fi

failed=()
for suite in "${suites[@]}"; do
    name="$(basename "$suite")"
    echo "=== $name"
    if bash "$suite"; then
        echo "--- PASS $name"
    else
        echo "--- FAIL $name"
        failed+=("$name")
    fi
done

echo
echo "$(( ${#suites[@]} - ${#failed[@]} ))/${#suites[@]} shell suites passed"
if [ "${#failed[@]}" -gt 0 ]; then
    printf 'FAILED: %s\n' "${failed[@]}"
    exit 1
fi
