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
#      test_reranker_installers.sh);
#   4. Step 0 refuses to resolve the real user to root. `sudo bash install.sh`
#      from a root shell sets SUDO_USER=root, which passed the POSIX-name check
#      and ran every as-the-user step (venvs, rustup/cargo/npm, the units'
#      User=) as root — the root-owned build trees O22-03 is about.
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

# ── 4. Step 0 never resolves REAL_USER to root ────────────────────────────────
# Runs install.sh's real Step 0 — and NOTHING after it — with $EUID swapped for
# a variable, so the sudo branch can be exercised unprivileged. The extract
# stops at the first `_root_ok_re=` line (awk exits there: a sed range would
# re-open at "Step 0a" and pull in the rest of the installer), is checked to be
# short and free of privileged commands BEFORE it runs, and runs with a `sudo`
# first on PATH that refuses everything.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STEP0="$WORK/step0.sh"
awk '/^# ── Step 0: /{on=1} on && /^_root_ok_re=/{exit} on' "$INSTALL_SH" \
    | sed 's/\$EUID/$FAKE_EUID/g' > "$STEP0"
grep -q 'REAL_USER="\$SUDO_USER"' "$STEP0" || fail "4: could not extract Step 0 from install.sh"
[[ "$(wc -l < "$STEP0")" -lt 80 ]] || fail "4: the Step 0 extract is $(wc -l < "$STEP0") lines — refusing to run more than Step 0"
grep -nE '^[[:space:]]*(sudo|install|rm|git|apt|systemctl|usermod)[[:space:]]|\$\(sudo|\|[[:space:]]*sudo' "$STEP0" \
    && fail "4: the Step 0 extract contains privileged commands (above) — refusing to run it"
mkdir -p "$WORK/bin"
printf '#!/bin/sh\necho "refused: sudo $*" >&2\nexit 99\n' > "$WORK/bin/sudo"
chmod +x "$WORK/bin/sudo"
step0() {  # <fake euid> <SUDO_USER> <USER> — prints the exit code
    PATH="$WORK/bin:$PATH" FAKE_EUID="$1" SUDO_USER="$2" USER="$3" HOME=/nonexistent \
        bash -euo pipefail "$STEP0" > "$WORK/step0.log" 2>&1
    echo $?
}
rc="$(step0 0 root root)"
[[ "$rc" == 1 ]] || fail "4: 'sudo bash install.sh' from a root shell (SUDO_USER=root) was not refused (rc=$rc): $(cat "$WORK/step0.log")"
grep -qi 'run as your user\|run it as your user' "$WORK/step0.log" \
    || fail "4: the refusal does not tell the operator what to do: $(cat "$WORK/step0.log")"
rc="$(step0 1000 '' root)"
[[ "$rc" == 1 ]] || fail "4: a non-sudo run with USER=root was not refused (rc=$rc)"
rc="$(step0 0 nobody root)"
[[ "$rc" == 0 ]] || fail "4: 'sudo bash install.sh' by a normal user (SUDO_USER=nobody) was refused (rc=$rc): $(cat "$WORK/step0.log")"
grep -q 'REAL_USER=nobody' "$WORK/step0.log" || fail "4: Step 0 did not resolve REAL_USER from SUDO_USER"
grep -q 'refused: sudo' "$WORK/step0.log" && fail "4: Step 0 tried to run sudo"
ROOT_GATE_AT="$(grep -nE '^if .*"\$REAL_USER" == root' "$INSTALL_SH" | head -n1 | cut -d: -f1)"
[[ -n "$ROOT_GATE_AT" && "$ROOT_GATE_AT" -lt "$SUDO_AT" ]] \
    || fail "4: the REAL_USER=root gate (line ${ROOT_GATE_AT:-none}) must run before install.sh's first sudo (line $SUDO_AT)"

echo "ALL TESTS PASSED"
exit 0
