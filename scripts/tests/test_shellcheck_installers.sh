#!/usr/bin/env bash
# Lint gate: ShellCheck over the installer scripts, Scripts/*.sh and
# installer/templates/*.sh (globbed, so a new script is covered automatically).
#
# 2026-10-09 audit rank 40 asked for it, and the first run earned it: shellcheck
# could not parse install.sh past the blackbox.service heredoc, because two
# unescaped backtick pairs in a comment there were command substitutions — run
# while install.sh wrote the unit, and missing from the deployed file. Nothing
# after that line had ever been analysed.
#
# Every finding fails the test except the pre-existing ones in EXCLUDE below,
# each matched by file, code AND message text (never a blanket code), so a new
# instance of the same warning still fails. An exclusion that no longer matches
# anything also fails: delete it when its finding is fixed.
#
# Binary: $SHELLCHECK, else shellcheck on PATH (preinstalled on GitHub's
# Ubuntu runners). Without either the test prints SKIP and passes, unless
# SHELLCHECK_REQUIRED=1 (set it in CI so a missing binary cannot pass silently).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

fail() { echo "FAIL: $*" >&2; exit 1; }

SC="${SHELLCHECK:-$(command -v shellcheck || true)}"
if [[ -z "$SC" || ! -x "$SC" ]]; then
    [[ "${SHELLCHECK_REQUIRED:-0}" == 1 ]] && fail "shellcheck not found (SHELLCHECK_REQUIRED=1)"
    echo "SKIP: shellcheck not installed (set SHELLCHECK=/path/to/shellcheck to run this gate)"
    exit 0
fi

# <file>|<code>|<message substring> — pre-existing findings not fixed by the
# 2026-10 installer pass.
EXCLUDE=(
    # retry-loop counter that is never read
    "Scripts/auto-apn.sh|SC2034|i appears unused"
    # `cmd && echo ok || echo failed` status lines (two of them)
    "Scripts/auto-apn.sh|SC2015|A && B || C is not if-then-else"
    # cert paths left declared by the zellij TLS walk-back (Step 2c, see the
    # "TLS posture" note there)
    "Scripts/install.sh|SC2034|CERT_FILE appears unused"
    "Scripts/install.sh|SC2034|KEY_FILE appears unused"
)

cd "$REPO_ROOT" || fail "cannot cd to $REPO_ROOT"
shopt -s nullglob
FILES=(Scripts/*.sh installer/templates/*.sh)
shopt -u nullglob
[[ "${#FILES[@]}" -gt 0 ]] || fail "no installer scripts found under Scripts/ or installer/templates/"
for must in Scripts/install.sh installer/templates/blackbox-install-localstack.sh \
            installer/templates/blackbox-install-reranker.sh; do
    printf '%s\n' "${FILES[@]}" | grep -qxF "$must" || fail "$must is not covered by the glob"
done

OUT="$("$SC" --format=gcc --severity=style "${FILES[@]}" 2>&1)"; RC=$?
# 0 = clean, 1 = findings; anything else is shellcheck itself failing.
[[ "$RC" -le 1 ]] || fail "shellcheck exited $RC: $OUT"

declare -A USED=()
LEFT=()
while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    matched=0
    for ex in "${EXCLUDE[@]}"; do
        IFS='|' read -r ex_file ex_code ex_msg <<< "$ex"
        if [[ "$line" == "$ex_file:"* && "$line" == *"[$ex_code]"* && "$line" == *"$ex_msg"* ]]; then
            USED["$ex"]=1; matched=1; break
        fi
    done
    (( matched )) || LEFT+=("$line")
done <<< "$OUT"

if [[ "${#LEFT[@]}" -gt 0 ]]; then
    printf '%s\n' "${LEFT[@]}" >&2
    fail "shellcheck found ${#LEFT[@]} new issue(s) in the installer scripts (above)"
fi
for ex in "${EXCLUDE[@]}"; do
    [[ -n "${USED[$ex]:-}" ]] || fail "stale exclusion (nothing matches it any more — delete it): $ex"
done

echo "ALL TESTS PASSED (shellcheck $("$SC" --version | awk '/^version:/ {print $2}'), ${#FILES[@]} scripts)"
exit 0
