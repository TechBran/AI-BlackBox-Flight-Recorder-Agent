#!/usr/bin/env bash
# Test for the install-root / home charset gate in install.sh and in the two
# installer scripts that template the same paths.
#
# 2026-10-09 audit X04-security-clients-infra-14 / O22-02 (rank 40): install.sh
# admitted spaces, `&`, `()` and `%` in BLACKBOX_ROOT on the theory that every
# substitution site escapes them. The systemd heredocs do not: ExecStart=,
# WorkingDirectory=, ReadWritePaths= and Environment= take the root unquoted, so
# a space splits the value (226/NAMESPACE, 203/EXEC) and `%` is expanded as a
# specifier. The localstack script templated the root into a single-quoted
# `sh -c '…'` (PYTHONPATH=…) with no check at all, where `:` also splits the
# path list. $REAL_HOME reaches the same unit lines.
#
# Proves:
#   1. install.sh's _root_ok_re accepts the real install roots (the dev box and
#      MS02 trees both contain a `.`-suffixed directory) and rejects every
#      character a unit file, sed program, sh string or path list mis-handles;
#   2. install.sh gates BLACKBOX_ROOT and REAL_HOME before its first sudo;
#   3. the localstack and reranker scripts enforce the identical charset (their
#      behavioural refusals are covered by test_localstack_cuda_build.sh and
#      test_reranker_installers.sh).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
LOCALSTACK_SH="$REPO_ROOT/installer/templates/blackbox-install-localstack.sh"
RERANKER_SH="$REPO_ROOT/installer/templates/blackbox-install-reranker.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

RE_LINE="$(grep -E '^_root_ok_re=' "$INSTALL_SH" | head -n 1)"
[[ -n "$RE_LINE" ]] || fail "install.sh no longer defines _root_ok_re"
eval "$RE_LINE"   # defines _root_ok_re exactly as install.sh does
# shellcheck disable=SC2154
: "${_root_ok_re:?}"

# ── 1. accept / reject ────────────────────────────────────────────────────────
for ok in "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" \
          "/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main" \
          "/opt/black_box+v2@lab,b~x/repo" "/home/bbx"; do
    [[ "$ok" =~ $_root_ok_re ]] || fail "a legitimate path is rejected: $ok"
done
NL=$'\n'; TAB=$'\t'
for bad in "/home/r&d/repo" "/home/a b/repo" "/srv/repo(2)" "/srv/50%off" "/srv/a:b" \
           "/srv/q'uote" '/srv/d"q' '/srv/$HOME' '/srv/`id`' '/srv/back\slash' \
           "/srv/semi;colon" "/srv/pi|pe" "/srv/new${NL}line" "/srv/t${TAB}ab" \
           "/srv/glob*" "/srv/q?" "/srv/[x]" "/srv/<x>" "/srv/{a,b}" "/srv/x#y" \
           "/srv/ex!cl" "/srv/eq=x" "relative/path" ""; do
    [[ "$bad" =~ $_root_ok_re ]] && fail "an unsafe path is accepted: '$bad'"
done

# ── 2. install.sh gates both paths before its first privileged command ───────
grep -qE '^if \[\[ ! "\$BLACKBOX_ROOT" =~ \$_root_ok_re \]\]; then' "$INSTALL_SH" \
    || fail "install.sh no longer gates BLACKBOX_ROOT with _root_ok_re"
grep -qE '^if \[\[ ! "\$REAL_HOME" =~ \$_root_ok_re \]\]; then' "$INSTALL_SH" \
    || fail "install.sh does not gate REAL_HOME (templated into the cli-agent drop-in's ReadWritePaths)"
GATE_AT="$(grep -nE '^if \[\[ ! "\$REAL_HOME" =~ \$_root_ok_re \]\]; then' "$INSTALL_SH" | head -n1 | cut -d: -f1)"
SUDO_AT="$(grep -nE '^[[:space:]]*sudo ' "$INSTALL_SH" | head -n1 | cut -d: -f1)"
[[ -n "$SUDO_AT" && "$GATE_AT" -lt "$SUDO_AT" ]] \
    || fail "the path gates (line $GATE_AT) must run before install.sh's first sudo (line $SUDO_AT)"

# ── 3. the installer scripts use the same charset ─────────────────────────────
WANT="${RE_LINE#_root_ok_re=}"
for f in "$LOCALSTACK_SH" "$RERANKER_SH"; do
    line="$(grep -E '^_path_ok_re=' "$f" | head -n1)"
    [[ -n "$line" ]] || fail "$(basename "$f") has no _path_ok_re charset gate"
    [[ "${line#_path_ok_re=}" == "$WANT" ]] \
        || fail "$(basename "$f") charset '${line#_path_ok_re=}' differs from install.sh's $WANT"
done

echo "ALL TESTS PASSED"
exit 0
