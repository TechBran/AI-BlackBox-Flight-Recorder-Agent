#!/usr/bin/env bash
# Test for Orchestrator/asterisk/configs/install.sh (audit W05).
#
# The ARI password used to be a literal in this script and setup.sh. Now its
# only home is ASTERISK_ARI_PASSWORD in .env: generated when empty, replaced on
# --rotate-ari-password, and rendered into ari.conf. Runs the real script in a
# mktemp sandbox (ASTERISK_ETC + BLACKBOX_ROOT overrides) with systemctl,
# asterisk, chown, journalctl and curl stubbed on PATH, so no real /etc, no
# root and no running Asterisk are needed. Asserts the password never reaches
# stdout or curl's argv. IVR prompts are not this script's job: the service
# writes them into the prompt dir Scripts/install.sh makes for it.
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

# Sandbox BlackBox root: copies of the two scripts + the real env_key.py helper,
# a template-style .env, and a per-box pjsip.conf next to the scripts (a full
# install copies it; an ARI rotation must not).
CONFIGS="$WORK/root/Orchestrator/asterisk/configs"
mkdir -p "$WORK/root/Scripts" "$CONFIGS" "$WORK/etc/asterisk" "$WORK/bin"
cp "$REPO_ROOT/Scripts/env_key.py" "$WORK/root/Scripts/env_key.py"
cp "$SCRIPT" "$SETUP" "$CONFIGS/"
SCRIPT="$CONFIGS/install.sh"
SETUP="$CONFIGS/setup.sh"
printf 'ASTERISK_ARI_USER=blackbox\nASTERISK_ARI_PASSWORD=\nOTHER=keep\n' > "$WORK/root/.env"
chmod 600 "$WORK/root/.env"
printf '; repo pjsip\n' > "$CONFIGS/pjsip.conf"

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
# ARI is server-to-server on loopback (the Orchestrator's aiohttp client sends
# no Origin): no browser origin may get CORS on it (audit M3 U20 item 7).
grep -qiE '^[[:space:]]*allowed_origins[[:space:]]*=' "$WORK/etc/asterisk/ari.conf" \
    && fail "ari.conf grants CORS to browser origins (ARI must stay server-to-server)"
grep -qx "enabled=yes" "$WORK/etc/asterisk/ari.conf" || fail "ari.conf no longer enables ARI"
[[ "$(env_val OTHER)" == "keep" ]] || fail ".env lost other keys"
[[ "$(stat -c %a "$WORK/root/.env")" == "600" ]] || fail ".env mode changed"
# -- : a generated password may start with "-", which grep would take as an option.
grep -qF -- "$PW1" "$WORK/out.log" && fail "the ARI password was printed"
grep -qF -- "$PW1" "$WORK/curl.argv" && fail "the ARI password was on curl's command line"
grep -qF -- "$PW1" "$WORK/curl.stdin" || fail "curl did not get the credentials on stdin"
grep -q "ARI is responding" "$WORK/out.log" || fail "verification step did not run"
[[ "$(cat "$WORK/etc/asterisk/pjsip.conf")" == "; repo pjsip" ]] || fail "full install did not copy pjsip.conf"
grep -q 'install_prompts' "$SCRIPT" && fail "install.sh still runs a root prompt installer"

# ── Run 2: existing password is kept ──────────────────────────────────────────
run || fail "install.sh failed on second run"
[[ "$(env_val ASTERISK_ARI_PASSWORD)" == "$PW1" ]] || fail "an existing ARI password was replaced"

# ── Run 3: --rotate-ari-password replaces it (setup.sh delegates) ────────────
printf '; installed pjsip\n' > "$WORK/etc/asterisk/pjsip.conf"
BAKS_BEFORE="$(ls "$WORK/etc/asterisk/" | grep -c '^pjsip.conf.bak')"
PATH="$WORK/bin:$PATH" ASTERISK_ETC="$WORK/etc/asterisk" BLACKBOX_ROOT="$WORK/root" \
    bash "$SETUP" --rotate-ari-password > "$WORK/out.log" 2>&1 || fail "setup.sh --rotate-ari-password failed"
[[ "$(cat "$WORK/etc/asterisk/pjsip.conf")" == "; installed pjsip" ]] \
    || fail "--rotate-ari-password overwrote pjsip.conf (rotation must touch ari.conf only)"
[[ "$(ls "$WORK/etc/asterisk/" | grep -c '^pjsip.conf.bak')" == "$BAKS_BEFORE" ]] \
    || fail "rotation backed up a conf it does not touch"
PW2="$(env_val ASTERISK_ARI_PASSWORD)"
[[ -n "$PW2" && "$PW2" != "$PW1" ]] || fail "--rotate-ari-password did not rotate"
grep -qx "password=$PW2" "$WORK/etc/asterisk/ari.conf" || fail "ari.conf not re-rendered after rotation"
grep -qF -- "$PW2" "$WORK/out.log" && fail "the rotated ARI password was printed"

# ── Missing .env fails loudly ─────────────────────────────────────────────────
rm "$WORK/root/.env"
run && fail "install.sh should refuse to run without .env"

echo "PASS: test_asterisk_configs_install.sh"
