#!/usr/bin/env bash
# Scripts/update.sh's pinned-origin and signed-commit gate (2026-10 audit rank
# 79, X04-security-clients-infra-11), on its own functions: real git, real SSH
# signatures (ssh-keygen), a sandbox trust directory standing in for
# /etc/blackbox. (Orchestrator/tests/test_update/test_update_sh.py runs the
# whole script end to end; this is the shell job's own proof of the gate.)
#   1. check_pinned_origin: an unpinned box passes with a warning; the pinned
#      origin passes whatever its case/.git/slash; another origin, an empty pin,
#      an insteadOf rewrite and an untrusted pin (other owner, group/world
#      writable, symlink, writable directory) refuse with exit 4;
#   2. verify_release_commit: no key pinned passes with a warning; with a key,
#      an unsigned commit, a commit signed by another key, and a commit whose
#      .git/config plants an attacker's signers file or an always-good
#      verifier all refuse with exit 4; the pinned key's commit passes.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UPDATE_SH="$REPO_ROOT/Scripts/update.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
command -v ssh-keygen > /dev/null || { echo "SKIP: ssh-keygen not installed"; exit 0; }

WORK="$(mktemp -d)"
trap 'chmod -R u+rwx "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

# ── the gate's functions, with the trust directory moved into the sandbox ─────
GATE="$WORK/gate.sh"
TRUST="$WORK/etc-blackbox"
awk '/^# ── Pinned origin \+ release signature/{on=1} on && /^check_pinned_origin$/{exit} on' "$UPDATE_SH" \
    | sed -e "s|^UPDATE_TRUST_DIR=/etc/blackbox\$|UPDATE_TRUST_DIR=$TRUST|" \
          -e "s|^UPDATE_TRUST_UID=0\$|UPDATE_TRUST_UID=$(id -u)|" > "$GATE"
grep -q "^UPDATE_TRUST_DIR=$TRUST\$" "$GATE" && grep -q "^UPDATE_TRUST_UID=$(id -u)\$" "$GATE" \
    || fail "could not extract the gate from update.sh (or point it at the sandbox)"
grep -q '^verify_release_commit()' "$GATE" || fail "verify_release_commit() is not in the extract"

G=/usr/bin/git
[[ -x "$G" ]] || G="$(command -v git)"
q() { "$G" -C "$1" "${@:2}" >/dev/null 2>&1; }

mkdir -p "$TRUST" && chmod 0755 "$TRUST"
"$G" init -q --bare "$WORK/origin.git"
"$G" init -q "$WORK/box"
q "$WORK/box" config user.email t@example.invalid
q "$WORK/box" config user.name test
q "$WORK/box" config commit.gpgsign false
q "$WORK/box" remote add origin "$WORK/origin.git"
echo 1 > "$WORK/box/f"
q "$WORK/box" add f
q "$WORK/box" commit -m unsigned
UNSIGNED="$("$G" -C "$WORK/box" rev-parse HEAD)"
for k in release stranger; do
    ssh-keygen -q -t ed25519 -N "" -C "$k" -f "$WORK/$k"
    printf '%s@example.invalid namespaces="git" %s\n' "$k" "$(cut -d' ' -f1,2 "$WORK/$k.pub")" > "$WORK/$k.signers"
done
sign() {  # <key> → prints the new commit
    echo "$1" >> "$WORK/box/f"
    q "$WORK/box" add f
    q "$WORK/box" -c gpg.format=ssh -c user.signingkey="$WORK/$1" commit -S -m "signed by $1"
    "$G" -C "$WORK/box" rev-parse HEAD
}
SIGNED="$(sign release)"
FOREIGN="$(sign stranger)"

run_gate() {  # <function> [args]: in the box, prints output; exit status = the gate's
    ( cd "$WORK/box" || exit 99
      BLACKBOX_ROOT="$WORK/box"; TREE_OWNER="$(id -un)"
      # shellcheck source=/dev/null
      source "$GATE"
      "$@" )
}
expect() {  # <want rc> <what> <ERE the output must match> <function> [args]
    local want="$1" what="$2" re="$3" out rc
    shift 3
    out="$(run_gate "$@" 2>&1)"; rc=$?
    [[ "$rc" -eq "$want" ]] || fail "$what: exit $rc, expected $want: $out"
    grep -qE "$re" <<< "$out" || fail "$what: output does not match /$re/: $out"
}
pin_origin() { printf '# test pin\n%s\n' "$@" > "$TRUST/update-origin"; chmod 0644 "$TRUST/update-origin"; }

# ── 1. the origin ────────────────────────────────────────────────────────────
expect 0 "an unpinned box" "WARNING: no update origin is pinned" check_pinned_origin
pin_origin "https://github.com/example/elsewhere.git" "$(tr '[:lower:]' '[:upper:]' <<< "${WORK}/origin")/"
expect 0 "the pinned origin, upper-cased, no .git, trailing /" '^$' check_pinned_origin
pin_origin "https://github.com/example/elsewhere.git"
expect 4 "another origin" "REFUSED: this checkout fetches updates from $WORK/origin.git" check_pinned_origin
pin_origin
expect 4 "an empty pin" "lists no update origin" check_pinned_origin
pin_origin "https://github.com/example/the-real-repo.git"
q "$WORK/box" remote set-url origin "https://github.com/example/the-real-repo.git"
q "$WORK/box" config "url.$WORK/origin.git.insteadOf" "https://github.com/example/the-real-repo.git"
expect 4 "an insteadOf rewrite" "REFUSED" check_pinned_origin
q "$WORK/box" config --unset "url.$WORK/origin.git.insteadOf"
q "$WORK/box" remote set-url origin "$WORK/origin.git"
pin_origin "$WORK/origin.git"
chmod 0664 "$TRUST/update-origin"
expect 4 "a group-writable pin" "not a trusted pin" check_pinned_origin
chmod 0644 "$TRUST/update-origin"
chmod 0777 "$TRUST"
expect 4 "a world-writable trust directory" "not a trusted pin" check_pinned_origin
chmod 0755 "$TRUST"
mv "$TRUST/update-origin" "$WORK/elsewhere-origin"
ln -s "$WORK/elsewhere-origin" "$TRUST/update-origin"
expect 4 "a symlinked pin" "not a trusted pin" check_pinned_origin
rm "$TRUST/update-origin"
pin_origin "$WORK/origin.git"
OTHER_UID="$(( $(id -u) + 1 ))"
out="$( (cd "$WORK/box" && BLACKBOX_ROOT="$WORK/box" TREE_OWNER=x
         # shellcheck source=/dev/null
         source <(sed "s|^UPDATE_TRUST_UID=.*|UPDATE_TRUST_UID=$OTHER_UID|" "$GATE"); check_pinned_origin) 2>&1)"; rc=$?
[[ "$rc" -eq 4 ]] && grep -q "not a trusted pin" <<< "$out" || fail "a pin not owned by the trusted uid passed (rc=$rc): $out"
expect 0 "the pinned origin" '^$' check_pinned_origin

# ── 2. the release signature ─────────────────────────────────────────────────
expect 0 "no key pinned" "WARNING: no release signing key is pinned" verify_release_commit "$UNSIGNED"
grep -v '^ *$' "$WORK/release.signers" > "$TRUST/update-allowed-signers"
chmod 0644 "$TRUST/update-allowed-signers"
expect 4 "an unsigned commit" "not signed with a release key" verify_release_commit "$UNSIGNED"
expect 4 "a commit signed by another key" "not from a pinned release key" verify_release_commit "$FOREIGN"
expect 0 "the pinned key's commit" "is signed by a pinned release key" verify_release_commit "$SIGNED"
q "$WORK/box" config gpg.ssh.allowedSignersFile "$WORK/stranger.signers"
"$G" -C "$WORK/box" verify-commit "$FOREIGN" >/dev/null 2>&1 \
    || fail "precondition: the planted signers file should fool plain git"
expect 4 "a planted allowedSignersFile" "not from a pinned release key" verify_release_commit "$FOREIGN"
printf '#!/bin/sh\necho stranger@example.invalid\necho %s\nexit 0\n' "'Good \"git\" signature'" > "$WORK/always-good"
chmod +x "$WORK/always-good"
q "$WORK/box" config gpg.ssh.program "$WORK/always-good"
expect 4 "a planted always-good verifier" "REFUSED" verify_release_commit "$FOREIGN"
q "$WORK/box" config --unset gpg.ssh.program
q "$WORK/box" config --unset gpg.ssh.allowedSignersFile
printf '# only comments\n\n' > "$TRUST/update-allowed-signers"
expect 0 "a key file with only comments" "holds no key" verify_release_commit "$UNSIGNED"

echo "ALL TESTS PASSED"
exit 0
