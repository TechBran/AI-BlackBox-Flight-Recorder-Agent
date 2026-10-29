#!/usr/bin/env bash
# install.sh's update trust pins and its --publish-store-only mode
# (2026-10 audit rank 79, X04-security-clients-infra-11).
#
# publish_update_pins() copies the tracked installer/update-origin and
# installer/allowed_signers to the root-owned /etc/blackbox/update-origin and
# /etc/blackbox/update-allowed-signers, which Scripts/update.sh and
# Orchestrator/update/verify.py trust. This proves:
#   1. both pins land with mode 0644, byte-identical to the tracked files, in a
#      directory nobody else can write (the updaters refuse one that is);
#   2. the production destination is pinned to root:root + sudo;
#   3. the tracked files are well-formed: at least one https origin, and every
#      key line a valid ALLOWED SIGNERS entry (key type + base64 blob);
#   4. the shipped key count is reported (none today: not enforced yet);
#   5. `install.sh --publish-store-only` runs the REAL script end to end into a
#      sandbox and stops after the trust anchors: no sudo, no apt, no unit;
#   6. Scripts/update.sh's detection of that mode matches install.sh's parser.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
UPDATE_SH="$REPO_ROOT/Scripts/update.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FUNC_FILE="$WORK/func.sh"
sed -n '/^publish_update_pins()[[:space:]]*{/,/^}/p' "$INSTALL_SH" > "$FUNC_FILE"
grep -q 'publish_update_pins()' "$FUNC_FILE" || fail "could not extract publish_update_pins() from install.sh"

BLACKBOX_ROOT="$REPO_ROOT"
ETC_BLACKBOX_DIR="$WORK/etc/blackbox"
INSTALL_SUDO=""
INSTALL_OWNER="$(id -un)"
INSTALL_GROUP="$(id -gn)"
# shellcheck source=/dev/null
source "$FUNC_FILE"

# ── 1. the pins land, verbatim, 0644, in a directory nobody else can write ────
mkdir -p "$ETC_BLACKBOX_DIR"
chmod 0777 "$ETC_BLACKBOX_DIR"
OUT="$(publish_update_pins 2>&1)" || fail "publish_update_pins failed: $OUT"
for pair in "update-origin:installer/update-origin" "update-allowed-signers:installer/allowed_signers"; do
    pin="$ETC_BLACKBOX_DIR/${pair%%:*}"; src="$REPO_ROOT/${pair#*:}"
    [[ -f "$pin" ]] || fail "${pair%%:*} was not published"
    [[ "$(stat -c %a "$pin")" == 644 ]] || fail "${pair%%:*} mode is $(stat -c %a "$pin"), expected 644"
    cmp -s "$pin" "$src" || fail "${pair%%:*} is not a verbatim copy of ${pair#*:}"
done
(( 8#$(stat -c %a "$ETC_BLACKBOX_DIR") & 8#022 )) \
    && fail "the pin directory is still group/world-writable ($(stat -c %a "$ETC_BLACKBOX_DIR")): the updaters would refuse every update"
publish_update_pins >/dev/null 2>&1 || fail "publish_update_pins is not idempotent"

# ── 2. production is root:root via sudo, whatever the environment says ────────
grep -q 'if \[\[ "\$ETC_BLACKBOX_DIR" == "/etc/blackbox" \]\]; then' "$FUNC_FILE" \
    || fail "publish_update_pins no longer pins the production /etc/blackbox destination"
grep -q 'OWNER=root; GROUP=root; SUDO=sudo' "$FUNC_FILE" \
    || fail "publish_update_pins' production branch no longer forces root:root + sudo"

# ── 3. the tracked sources are well-formed ────────────────────────────────────
mapfile -t ORIGINS < <(grep -vE '^[[:space:]]*(#|$)' "$REPO_ROOT/installer/update-origin")
[[ "${#ORIGINS[@]}" -ge 1 ]] || fail "installer/update-origin lists no origin: every update would be refused"
for o in "${ORIGINS[@]}"; do
    [[ "$o" =~ ^https://[^[:space:]]+$ ]] || fail "installer/update-origin has a non-https or malformed origin: '$o'"
done
KEY_TYPES='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)$'
KEYS=0
while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    read -r -a f <<< "$line"
    found=0
    for ((i = 1; i < ${#f[@]} - 1; i++)); do
        if [[ "${f[i]}" =~ $KEY_TYPES ]]; then
            printf '%s' "${f[i+1]}" | base64 -d > /dev/null 2>&1 \
                || fail "installer/allowed_signers: the key after ${f[i]} is not base64: $line"
            found=1
            break
        fi
    done
    [[ "$found" -eq 1 ]] || fail "installer/allowed_signers: not an ALLOWED SIGNERS entry (principal, options, key type, key): $line"
    KEYS=$((KEYS + 1))
done < "$REPO_ROOT/installer/allowed_signers"

# ── 4. the key count is reported ──────────────────────────────────────────────
if [[ "$KEYS" -eq 0 ]]; then
    grep -q 'No release signing key is pinned yet' <<< "$OUT" || fail "an empty key file is not reported as unenforced: $OUT"
else
    grep -q "Pinned $KEYS release signing key" <<< "$OUT" || fail "the key count is not reported: $OUT"
fi
KEYED="$WORK/keyed"
mkdir -p "$KEYED/installer"
cp "$REPO_ROOT/installer/update-origin" "$KEYED/installer/"
{ cat "$REPO_ROOT/installer/allowed_signers"
  echo 'releases@example.invalid namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGm2jzGPpJb1HuM4w5HnpJ2J0wq1yW8Gkq3v5n3n3n3n'
} > "$KEYED/installer/allowed_signers"
OUT2="$(BLACKBOX_ROOT="$KEYED" ETC_BLACKBOX_DIR="$WORK/etc2" publish_update_pins 2>&1)" \
    || fail "publish_update_pins failed with a key: $OUT2"
grep -q 'Pinned 1 release signing key' <<< "$OUT2" || fail "one key is not reported: $OUT2"

# ── 5. --publish-store-only: the real script, stopped after the trust anchors ─
SANDBOX="$WORK/e2e"
mkdir -p "$SANDBOX/bin" "$SANDBOX/sbin" "$SANDBOX/etc/blackbox"
for cmd in sudo apt apt-get systemctl usermod; do
    printf '#!/bin/sh\necho "refused: %s $*" >&2\nexit 99\n' "$cmd" > "$SANDBOX/bin/$cmd"
done
chmod +x "$SANDBOX/bin/"*
E2E_OUT="$(PATH="$SANDBOX/bin:$PATH" SBIN_DIR="$SANDBOX/sbin" ETC_BLACKBOX_DIR="$SANDBOX/etc/blackbox" \
    INSTALL_SUDO="" INSTALL_OWNER="$(id -un)" INSTALL_GROUP="$(id -gn)" \
    bash "$INSTALL_SH" --publish-store-only 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]] || fail "install.sh --publish-store-only exited $RC: $E2E_OUT"
grep -q 'refused:' <<< "$E2E_OUT" && fail "--publish-store-only ran a privileged or system command: $E2E_OUT"
for f in sbin/blackbox-apt-install sbin/blackbox-write-systemd etc/blackbox/system-packages.txt \
         etc/blackbox/templates/sudoers-system etc/blackbox/update-origin etc/blackbox/update-allowed-signers; do
    [[ -f "$SANDBOX/$f" ]] || fail "--publish-store-only did not publish $f"
done
grep -q 'nothing else was changed' <<< "$E2E_OUT" || fail "--publish-store-only did not stop after the trust anchors: $E2E_OUT"
grep -qE 'Installing system packages|Installing blackbox.service' <<< "$E2E_OUT" \
    && fail "--publish-store-only went on into the full install: $E2E_OUT"
bash "$INSTALL_SH" --no-such-option >/dev/null 2>&1; RC=$?
[[ "$RC" -eq 2 ]] || fail "an unknown option is not refused with exit 2 (got $RC)"

# ── 6. update.sh recognises the mode by install.sh's parser arm ───────────────
DETECT="$(grep -oE "grep -qE '[^']*publish-store-only[^']*'" "$UPDATE_SH" | head -n 1 | sed -E "s/^grep -qE '(.*)'\$/\1/")"
[[ -n "$DETECT" ]] || fail "could not find update.sh's --publish-store-only detection"
grep -qE "$DETECT" "$INSTALL_SH" || fail "update.sh's detection '$DETECT' does not match install.sh's option parser"

echo "ALL TESTS PASSED"
exit 0
