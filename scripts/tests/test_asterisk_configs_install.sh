#!/usr/bin/env bash
# Test for Orchestrator/asterisk/configs/install.sh (audit W05).
#
# The ARI password used to be a literal in this script and setup.sh. Now its
# only home is ASTERISK_ARI_PASSWORD in .env: generated when empty, replaced on
# --rotate-ari-password, and rendered into ari.conf. Runs the real script in a
# mktemp sandbox (ASTERISK_ETC + BLACKBOX_ROOT overrides) with systemctl,
# asterisk, chown, journalctl and curl stubbed on PATH, so no real /etc, no
# root and no running Asterisk are needed. Asserts the password never reaches
# stdout or curl's argv.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/Orchestrator/asterisk/configs/install.sh"
SETUP="$REPO_ROOT/Orchestrator/asterisk/configs/setup.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$SCRIPT" ]] || fail "install.sh not found at $SCRIPT"
bash -n "$SCRIPT" || fail "install.sh does not parse"
bash -n "$SETUP" || fail "setup.sh does not parse"

# No credential literal may come back into either tracked script.
for f in "$SCRIPT" "$SETUP"; do
    grep -nE '(^|[^$])-u [A-Za-z0-9_]+:[^$<"]' "$f" && fail "$f passes a literal user:password to curl"
    grep -nE '^[[:space:]]*password=[^$]' "$f" && fail "$f contains a literal password= line"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Sandbox BlackBox root: the real env_key.py helper + a template-style .env.
mkdir -p "$WORK/root/Scripts" "$WORK/etc/asterisk" "$WORK/bin"
cp "$REPO_ROOT/Scripts/env_key.py" "$WORK/root/Scripts/env_key.py"
printf 'ASTERISK_ARI_USER=blackbox\nASTERISK_ARI_PASSWORD=\nOTHER=keep\n' > "$WORK/root/.env"
chmod 600 "$WORK/root/.env"
printf '[general]\nenabled=yes\n' > "$WORK/etc/asterisk/http.conf.seed"

# Stubs: record curl's argv and stdin; the rest succeed silently.
for cmd in systemctl asterisk chown journalctl; do
    printf '#!/bin/sh\nexit 0\n' > "$WORK/bin/$cmd"
done
cat > "$WORK/bin/curl" <<STUB
#!/bin/sh
echo "\$@" >> "$WORK/curl.argv"
cat >> "$WORK/curl.stdin"
printf 200
STUB
chmod +x "$WORK/bin/"*

run() {
    PATH="$WORK/bin:$PATH" ASTERISK_ETC="$WORK/etc/asterisk" BLACKBOX_ROOT="$WORK/root" \
        bash "$SCRIPT" "$@" > "$WORK/out.log" 2>&1
}

env_val() { python3 -I "$WORK/root/Scripts/env_key.py" get "$WORK/root/.env" "$1"; }

# ── Run 1: empty password -> generated into .env and rendered ────────────────
run || { cat "$WORK/out.log"; fail "install.sh failed on first run"; }
PW1="$(env_val ASTERISK_ARI_PASSWORD)"
[[ ${#PW1} -ge 32 ]] || fail "no strong ARI password generated in .env"
grep -qx "password=$PW1" "$WORK/etc/asterisk/ari.conf" || fail "ari.conf does not carry the .env password"
grep -qx "\[blackbox\]" "$WORK/etc/asterisk/ari.conf" || fail "ari.conf user section missing"
[[ "$(env_val OTHER)" == "keep" ]] || fail ".env lost other keys"
[[ "$(stat -c %a "$WORK/root/.env")" == "600" ]] || fail ".env mode changed"
grep -qF "$PW1" "$WORK/out.log" && fail "the ARI password was printed"
grep -qF "$PW1" "$WORK/curl.argv" && fail "the ARI password was on curl's command line"
grep -qF "$PW1" "$WORK/curl.stdin" || fail "curl did not get the credentials on stdin"
grep -q "ARI is responding" "$WORK/out.log" || fail "verification step did not run"

# ── Run 2: existing password is kept ──────────────────────────────────────────
run || fail "install.sh failed on second run"
[[ "$(env_val ASTERISK_ARI_PASSWORD)" == "$PW1" ]] || fail "an existing ARI password was replaced"

# ── Run 3: --rotate-ari-password replaces it (setup.sh delegates) ────────────
PATH="$WORK/bin:$PATH" ASTERISK_ETC="$WORK/etc/asterisk" BLACKBOX_ROOT="$WORK/root" \
    bash "$SETUP" --rotate-ari-password > "$WORK/out.log" 2>&1 || fail "setup.sh --rotate-ari-password failed"
PW2="$(env_val ASTERISK_ARI_PASSWORD)"
[[ -n "$PW2" && "$PW2" != "$PW1" ]] || fail "--rotate-ari-password did not rotate"
grep -qx "password=$PW2" "$WORK/etc/asterisk/ari.conf" || fail "ari.conf not re-rendered after rotation"
grep -qF "$PW2" "$WORK/out.log" && fail "the rotated ARI password was printed"

# ── Missing .env fails loudly ─────────────────────────────────────────────────
rm "$WORK/root/.env"
run && fail "install.sh should refuse to run without .env"

echo "PASS: test_asterisk_configs_install.sh"
