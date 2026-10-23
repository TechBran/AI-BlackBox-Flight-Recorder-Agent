#!/usr/bin/env bash
# Test for Scripts/install.sh Step 3a-tel: TELEPHONY_SECRET_KEY is generated
# at install when absent (audit W05), never replaced once set, and NOT generated
# when gateways.json already holds values encrypted under the built-in
# fallback key (a new key would make them unreadable).
#
# Like test_install_asterisk_block.sh: install.sh has no main() guard, so the
# block is EXTRACTED with sed and run in a mktemp sandbox, with `sudo -u USER`
# stubbed to run the command as the current user.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

BLOCK="$WORK/block.sh"
sed -n '/^# Step 3a-tel/,/^fi$/p' "$INSTALL_SH" > "$BLOCK"
grep -q 'env_key.py" generate --kind fernet' "$BLOCK" || fail "could not extract Step 3a-tel from install.sh"

mkdir -p "$WORK/root/Scripts" "$WORK/root/Orchestrator/asterisk" "$WORK/bin"
cp "$REPO_ROOT/Scripts/env_key.py" "$WORK/root/Scripts/env_key.py"
cat > "$WORK/bin/sudo" <<'STUB'
#!/bin/sh
[ "$1" = "-u" ] && shift 2
exec "$@"
STUB
chmod +x "$WORK/bin/sudo"

run_block() {
    PATH="$WORK/bin:$PATH" BLACKBOX_ROOT="$WORK/root" REAL_USER="$USER" \
        bash -c "set -e; source '$BLOCK'" > "$WORK/out.log" 2>&1
}
key() { python3 -I "$WORK/root/Scripts/env_key.py" get "$WORK/root/.env" TELEPHONY_SECRET_KEY; }

# ── fresh box: empty template line -> a Fernet key, printed nowhere ───────────
printf 'OPENAI_API_KEY=\nTELEPHONY_SECRET_KEY=\n' > "$WORK/root/.env"
chmod 600 "$WORK/root/.env"
run_block || { cat "$WORK/out.log"; fail "block failed on a fresh .env"; }
K1="$(key)"
[[ ${#K1} -eq 44 ]] || fail "no Fernet-shaped TELEPHONY_SECRET_KEY generated"
# -- : a generated key may start with "-", which grep would take as an option.
grep -qF -- "$K1" "$WORK/out.log" && fail "the generated key was printed"
[[ "$(grep -c '^TELEPHONY_SECRET_KEY=' "$WORK/root/.env")" == "1" ]] || fail "duplicate TELEPHONY_SECRET_KEY lines"

# ── re-run: a set key is never replaced ───────────────────────────────────────
run_block || fail "block failed on re-run"
[[ "$(key)" == "$K1" ]] || fail "an existing TELEPHONY_SECRET_KEY was replaced"

# ── fallback-encrypted gateways: warn, do not generate ────────────────────────
printf 'TELEPHONY_SECRET_KEY=\n' > "$WORK/root/.env"
printf '[{"id": "gw1", "ami": {"secret": "enc:gAAAAexample"}}]\n' > "$WORK/root/Orchestrator/asterisk/gateways.json"
run_block || fail "block failed with fallback-encrypted gateways"
[[ -z "$(key)" ]] || fail "generated a key that would orphan fallback-encrypted gateway secrets"
grep -q "WARN: TELEPHONY_SECRET_KEY is unset" "$WORK/out.log" || fail "no warning for fallback-encrypted gateways"

echo "PASS: test_install_telephony_secret_key.sh"
