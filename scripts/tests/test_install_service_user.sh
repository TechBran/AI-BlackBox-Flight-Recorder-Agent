#!/usr/bin/env bash
# install.sh's opt-in move to a dedicated service account (Step 0s; 2026-10
# audit rank 79, X04-security-clients-infra-12), proven on a throwaway tree.
#
# The real Step 0s functions run against a sandbox shaped like a box: a desktop
# user's 0750 home holding the checkout (Volumes/, Manifest/, data/,
# credentials/, Portal/uploads/, both venvs, .git, .env) and the BlackBox's own
# per-user state (.blackbox with a venv script, zellij tokens, agent
# workspaces) next to the user's own CLI sign-ins (.claude). The account
# database, sudo, useradd and systemctl are stubs; setfacl/getfacl are real
# (numeric uid). This proves:
#   1. --dry-run prints every change (useradd, each top-level chown, each move
#      and symlink, each traverse ACL, each unit stop, the recorded choice, the
#      display entry) and changes nothing at all;
#   2. the move does exactly that: the account, chown -h of every entry, the
#      state moved (the CLI sign-ins left), .blackbox linked back so old venv
#      paths resolve, traverse-only ACLs, the choice and display entry written,
#      REAL_USER/REAL_HOME switched and DESKTOP_USER kept;
#   3. a later run with the choice recorded has nothing to do and says nothing;
#   4. --revert-service-user (dry run, then for real) gives it all back and
#      removes the ACLs, the display entry and the choice;
#   5. refusals change nothing: an account in sudo, uid 0, a foreign primary
#      group, the desktop user itself, a bad name, an enabled vLLM reranker, a
#      different recorded account, a malformed choice file, a revert by someone
#      else;
#   6. with no choice the step is silent and leaves REAL_USER alone, and the
#      units, the CLI-agent drop-in, the sudoers and the store render for the
#      service account only when there is one (byte-identical otherwise);
#   7. the desktop-session steps use DESKTOP_USER and nothing adds anyone to sudo.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'chmod -R u+rwx "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

# ── the Step 0s block, functions and constants only ──────────────────────────
STEP="$WORK/step0s.sh"
awk '/^# ── Step 0s: /{on=1} on && /^resolve_service_account \/etc\/blackbox\/service-user/{exit} on' \
    "$INSTALL_SH" > "$STEP"
grep -q '^migrate_to_service_account()' "$STEP" || fail "could not extract Step 0s from install.sh"
# Only comments, assignments, function bodies and closing brackets at column 0
# (here-document bodies aside).
sed '/<<[A-Z_]*EOF$/,/^[A-Z_]*EOF$/d' "$STEP" \
    | grep -nvE '^($|#|[[:space:]]|[}()]|[A-Za-z_][A-Za-z0-9_]*=|[a-z_]+\(\) \{)' \
    && fail "the Step 0s extract runs something at top level (above): refusing to source it"
eval "$(grep -E '^_root_ok_re=' "$INSTALL_SH" | head -n 1)"

ME="$(id -un)"; MYUID="$(id -u)"; MYGID="$(id -g)"
SVC=bbsvc; SVCUID=4242
DESK="$WORK/home/$ME"
ROOT="$DESK/Desktop/blackbox"
SVC_HOME="$WORK/var/lib/$SVC"
CHOICE="$WORK/etc/blackbox/service-user"
export LOG="$WORK/stub.log" FAKE_PASSWD="$WORK/passwd" FAKE_GROUPS="$WORK/groups" REAL_TEST_USER="$ME"
: > "$LOG"
printf '%s:x:%s:%s::%s:/bin/bash\n' "$ME" "$MYUID" "$MYGID" "$DESK" > "$FAKE_PASSWD"
: > "$FAKE_GROUPS"

# ── stubs ────────────────────────────────────────────────────────────────────
BIN="$WORK/bin"; mkdir -p "$BIN"
cat > "$BIN/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$LOG"
if [[ "$1" == -u ]]; then shift 2; fi
case "$1" in
    chown) exit 0 ;;   # cannot give files away unprivileged: recorded only
    install)
        shift; args=()
        while (($#)); do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
        exec install "${args[@]}" ;;
esac
exec "$@"
STUB
cat > "$BIN/useradd" <<'STUB'
#!/usr/bin/env bash
echo "useradd $*" >> "$LOG"
name="${*: -1}" home=""
while (($#)); do case "$1" in --home-dir) home="$2"; shift 2 ;; *) shift ;; esac; done
echo "$name:x:${FAKE_NEW_UID:-4242}:${FAKE_NEW_UID:-4242}:AI BlackBox service:$home:/usr/sbin/nologin" >> "$FAKE_PASSWD"
echo "$name:${FAKE_NEW_UID:-4242}:$name" >> "$FAKE_GROUPS"
mkdir -p "$home"
STUB
cat > "$BIN/getent" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == passwd && -n "${2:-}" ]]; then
    grep -m1 "^$2:" "$FAKE_PASSWD" && exit 0
    exit 2
fi
exec /usr/bin/getent "$@"
STUB
cat > "$BIN/id" <<'STUB'
#!/usr/bin/env bash
opt="${1:-}" name="${2:-}"
if [[ -n "$name" ]]; then
    line="$(grep -m1 "^$name:" "$FAKE_PASSWD")" || { echo "id: '$name': no such user" >&2; exit 1; }
    IFS=: read -r _ _ uid gid _ <<< "$line"
    groups="$(grep -m1 "^$name:" "$FAKE_GROUPS" | cut -d: -f3)"
    case "$opt" in
        -u) echo "$uid" ;;
        -g) echo "$gid" ;;
        -G) echo "$gid" ;;
        -gn) echo "${FAKE_PRIMARY_GROUP:-$name}" ;;
        -nG) echo "${groups:-$name}" ;;
        -un) echo "$name" ;;
    esac
    exit 0
fi
exec /usr/bin/id "$@"
STUB
cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$LOG"
unit="${*: -1}"
case "$1" in
    is-active) for u in ${ACTIVE_UNITS:-}; do [[ "$u" == "$unit" ]] && exit 0; done; exit 3 ;;
    is-enabled) for u in ${ENABLED_UNITS:-}; do [[ "$u" == "$unit" ]] && exit 0; done; exit 1 ;;
esac
exit 0
STUB
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"
export ACTIVE_UNITS="blackbox.service zellij-web.service"
export ENABLED_UNITS=""

# ── the sandbox box ──────────────────────────────────────────────────────────
mkdir -p "$ROOT"/{Volumes,Manifest,data,credentials,Portal/uploads,Orchestrator/venv/bin,MCP/venv/bin,.git} \
         "$DESK"/.blackbox/localstack/speaches-venv/bin "$DESK"/.local/share/zellij \
         "$DESK"/.local/share/blackbox "$DESK"/.claude "$DESK"/agent-workspaces/t1 \
         "$WORK/var/lib" "$WORK/etc/blackbox"
printf '#!%s/.blackbox/localstack/speaches-venv/bin/python\n' "$DESK" > "$DESK/.blackbox/localstack/speaches-venv/bin/pip"
echo tokens > "$DESK/.local/share/zellij/tokens.db"
echo token > "$DESK/.local/share/blackbox/zellij-master.token"
echo signed-in > "$DESK/.claude/credentials"
echo "[core]" > "$ROOT/.git/config"
echo "KEY=x" > "$ROOT/.env"
echo ledger > "$ROOT/Volumes/SNAPSHOT_VOLUME.txt"
ln -s /etc/hostname "$ROOT/Portal/a-link"
chmod 0750 "$DESK"
chmod 0755 "$WORK/home" "$DESK/Desktop"
ENTRIES=(.env .git Manifest MCP Orchestrator Portal Volumes credentials data)

snapshot() { find "$WORK" -path "$LOG" -prune -o -printf '%p %m %u %l\n' | sort; getfacl -Rpn "$WORK" 2>/dev/null; }

REAL_USER="$ME"; REAL_HOME="$DESK"
# shellcheck source=/dev/null
source "$STEP"
SERVICE_HOME_BASE="$WORK/var/lib"
reset_flags() { DRY_RUN=0; REQUESTED_SERVICE_USER=""; REVERT_SERVICE_USER=0; SVC_STOPPED_UNITS=(); }

# ── 6a. no choice, no option: silent, REAL_USER untouched ─────────────────────
reset_flags
OUT="$(resolve_service_account "$CHOICE" "$ROOT" 1 2>&1)" || fail "6: the default path failed: $OUT"
[[ -z "$OUT" ]] || fail "6: the default path printed something: $OUT"
[[ ! -s "$LOG" ]] || fail "6: the default path ran something: $(cat "$LOG")"

# ── 1. --dry-run prints every change and makes none ──────────────────────────
BEFORE="$(snapshot)"
reset_flags; DRY_RUN=1; REQUESTED_SERVICE_USER="$SVC"
OUT="$( (resolve_service_account "$CHOICE" "$ROOT" 1) 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]] || fail "1: the dry run failed ($RC): $OUT"
[[ "$(snapshot)" == "$BEFORE" ]] || fail "1: the dry run changed the sandbox"
grep -q '^sudo ' "$LOG" && fail "1: the dry run ran sudo: $(grep '^sudo ' "$LOG")"
want() { grep -qF -- "$1" <<< "$OUT" || fail "1: the dry run does not list: $1"$'\n'"$OUT"; }
want "would run: sudo useradd --system --user-group --create-home --home-dir $SVC_HOME --shell /usr/sbin/nologin --comment AI\\ BlackBox\\ service $SVC"
want "would run: sudo chmod 0700 $SVC_HOME"
want "would run: sudo setfacl -m u:$SVC:x $DESK"
want "would run: sudo systemctl stop blackbox.service"
want "would run: sudo systemctl stop zellij-web.service"
grep -qF "systemctl stop blackbox-mcp.service" <<< "$OUT" && fail "1: a unit that is not running is stopped"
want "would run: sudo chown -h $SVC:$SVC $ROOT"
for e in "${ENTRIES[@]}"; do
    want "would run: sudo chown -hR $SVC:$SVC $ROOT/$e"
    want "  $e: "
done
want "would run: sudo mv $DESK/.blackbox $SVC_HOME/.blackbox"
want "would run: sudo ln -s $SVC_HOME/.blackbox $DESK/.blackbox"
want "would run: sudo mv $DESK/.local/share/zellij $SVC_HOME/.local/share/zellij"
want "would run: sudo mv $DESK/.local/share/blackbox $SVC_HOME/.local/share/blackbox"
want "would run: sudo mv $DESK/agent-workspaces $SVC_HOME/agent-workspaces"
grep -qF "$DESK/.claude " <<< "$OUT" && fail "1: the desktop user's own CLI sign-ins are moved"
want "$CHOICE"
want "blackbox-service-display.desktop"
want "--revert-service-user"

# ── 2. the move ──────────────────────────────────────────────────────────────
: > "$LOG"
reset_flags; REQUESTED_SERVICE_USER="$SVC"
OUT="$(resolve_service_account "$CHOICE" "$ROOT" 1 2>&1)" || fail "2: the move failed: $OUT"
resolve_service_account "$CHOICE" "$ROOT" 1 > /dev/null 2>&1 || fail "2: a second call failed"
[[ "$REAL_USER" == "$SVC" && "$REAL_HOME" == "$SVC_HOME" ]] || fail "2: REAL_USER/REAL_HOME are $REAL_USER/$REAL_HOME"
[[ "$DESKTOP_USER" == "$ME" && "$DESKTOP_HOME" == "$DESK" ]] || fail "2: DESKTOP_USER/HOME changed"
grep -qF "useradd --system --user-group --create-home --home-dir $SVC_HOME --shell /usr/sbin/nologin" "$LOG" \
    || fail "2: the account was not created as a nologin system account"
grep -qE "sudo .*(usermod|gpasswd|adduser).*sudo" "$LOG" && fail "2: the service account was added to a sudo group"
for e in "${ENTRIES[@]}"; do
    grep -qxF "sudo chown -hR $SVC:$SVC $ROOT/$e" "$LOG" || fail "2: $e was not given to $SVC"
done
grep -qxF "sudo chown -h $SVC:$SVC $ROOT" "$LOG" || fail "2: the checkout root was not given to $SVC"
grep -qxF "sudo systemctl stop blackbox.service" "$LOG" || fail "2: blackbox.service was not stopped first"
[[ " ${SVC_STOPPED_UNITS[*]} " == *" zellij-web.service "* ]] || fail "2: the stopped units are not remembered for Step 7"
[[ -f "$SVC_HOME/.local/share/zellij/tokens.db" && ! -e "$DESK/.local/share/zellij" ]] || fail "2: the zellij token store did not move"
[[ -f "$SVC_HOME/.local/share/blackbox/zellij-master.token" ]] || fail "2: .local/share/blackbox did not move"
[[ -d "$SVC_HOME/agent-workspaces/t1" ]] || fail "2: agent-workspaces did not move"
[[ -f "$DESK/.claude/credentials" && ! -e "$SVC_HOME/.claude" ]] || fail "2: the desktop user's own .claude must stay"
[[ -L "$DESK/.blackbox" && "$(readlink "$DESK/.blackbox")" == "$SVC_HOME/.blackbox" ]] || fail "2: .blackbox is not linked back"
[[ -f "$DESK/.blackbox/localstack/speaches-venv/bin/pip" && -d "$SVC_HOME/.blackbox/localstack" ]] \
    || fail "2: the old venv path no longer resolves"
[[ "$(stat -c %a "$SVC_HOME")" == 700 ]] || fail "2: the service home is not 0700"
getfacl -cpn "$DESK" | grep -qx "user:$SVCUID:--x" || fail "2: no traverse-only ACL on the desktop home: $(getfacl -cpn "$DESK")"
getfacl -cpn "$DESK/Desktop" | grep -q "^user:$SVCUID:" && fail "2: an ACL on a directory others can already traverse"
grep -qx "SERVICE_USER=$SVC" "$CHOICE" && grep -qx "PREVIOUS_USER=$ME" "$CHOICE" || fail "2: the choice is not recorded: $(cat "$CHOICE" 2>&1)"
grep -qx "Exec=xhost +SI:localuser:$SVC" "$DESK/.config/autostart/blackbox-service-display.desktop" \
    || fail "2: no display access entry for the desktop session"

# ── 3. later runs have nothing to do ─────────────────────────────────────────
# (The stub could not chown: pretend it did by giving the account this uid.)
sed -i "s/^$SVC:x:$SVCUID:$SVCUID:/$SVC:x:$MYUID:$MYGID:/" "$FAKE_PASSWD"
: > "$LOG"
reset_flags; REAL_USER="$ME"; REAL_HOME="$DESK"
OUT="$(resolve_service_account "$CHOICE" "$ROOT" 1 2>&1)" || fail "3: a later run failed: $OUT"
[[ "$OUT" == "[install] The BlackBox runs as the service account $SVC (home $SVC_HOME; recorded in $CHOICE)" ]] \
    || fail "3: a later run did something: $OUT"
grep -q '^sudo ' "$LOG" && fail "3: a later run ran sudo: $(cat "$LOG")"
sed -i "s/^$SVC:x:$MYUID:$MYGID:/$SVC:x:$SVCUID:$SVCUID:/" "$FAKE_PASSWD"

# ── 5. refusals change nothing ───────────────────────────────────────────────
printf '%s\n' "bbsudo:x:4300:4300::$WORK/var/lib/bbsudo:/usr/sbin/nologin" \
              "bbroot:x:0:4301::$WORK/var/lib/bbroot:/usr/sbin/nologin" \
              "bbgrp:x:4302:100::$WORK/var/lib/bbgrp:/usr/sbin/nologin" >> "$FAKE_PASSWD"
printf '%s\n' "bbsudo:4300:bbsudo sudo" >> "$FAKE_GROUPS"
refused() {  # <why> <message ERE> <assignments...>: resolve in a subshell, must exit 1, change nothing
    local why="$1" msg="$2" out rc before
    shift 2
    before="$(snapshot)"
    out="$( (reset_flags; REAL_USER="$ME"; REAL_HOME="$DESK"; for a in "$@"; do eval "$a"; done
             resolve_service_account "${REFUSE_CHOICE:-$WORK/none/service-user}" "$ROOT" 1) 2>&1)"; rc=$?
    [[ "$rc" -eq 1 ]] || fail "5: $why was not refused (rc=$rc): $out"
    grep -qE "$msg" <<< "$out" || fail "5: $why: the refusal does not say why: $out"
    [[ "$(snapshot)" == "$before" ]] || fail "5: refusing $why changed the sandbox"
}
refused "an account in sudo" "in the sudo group" 'REQUESTED_SERVICE_USER=bbsudo'
refused "a uid-0 account" "has uid 0" 'REQUESTED_SERVICE_USER=bbroot'
FAKE_PRIMARY_GROUP=users refused "a foreign primary group" "primary group is users" 'REQUESTED_SERVICE_USER=bbgrp'
refused "the desktop user" "must not be $ME" "REQUESTED_SERVICE_USER=$ME"
refused "a bad name" "not a POSIX user name" 'REQUESTED_SERVICE_USER=Bad.Name'
ENABLED_UNITS="vllm-reranker.service" refused "an enabled vLLM reranker" "vllm-reranker.service" 'REQUESTED_SERVICE_USER=bbnew'
REFUSE_CHOICE="$CHOICE" refused "a different recorded account" "already runs the BlackBox as $SVC" 'REQUESTED_SERVICE_USER=bbnew'
mkdir -p "$WORK/bad"; echo "SERVICE_USER=\$(reboot)" > "$WORK/bad/service-user"
REFUSE_CHOICE="$WORK/bad/service-user" refused "a malformed choice file" "malformed" 'true'
REFUSE_CHOICE="$CHOICE" refused "a revert by someone else" "run --revert-service-user as $ME" \
    'REVERT_SERVICE_USER=1' 'DESKTOP_USER=someone-else'

# ── 4. --revert-service-user: dry run, then for real ─────────────────────────
BEFORE="$(snapshot)"
: > "$LOG"
OUT="$( (reset_flags; DRY_RUN=1; REVERT_SERVICE_USER=1; resolve_service_account "$CHOICE" "$ROOT" 1) 2>&1)"; RC=$?
[[ "$RC" -eq 0 ]] || fail "4: the revert dry run failed ($RC): $OUT"
[[ "$(snapshot)" == "$BEFORE" ]] || fail "4: the revert dry run changed the sandbox"
grep -q '^sudo ' "$LOG" && fail "4: the revert dry run ran sudo"
for line in "would run: sudo rm -f $DESK/.blackbox" "would run: sudo mv $SVC_HOME/.blackbox $DESK/.blackbox" \
            "would run: sudo mv $SVC_HOME/.local/share/zellij $DESK/.local/share/zellij" \
            "would run: sudo setfacl -x u:$SVCUID $DESK" "would run: sudo rm -f $CHOICE" \
            "would run: sudo rm -f $DESK/.config/autostart/blackbox-service-display.desktop"; do
    grep -qF -- "$line" <<< "$OUT" || fail "4: the revert dry run does not list: $line"$'\n'"$OUT"
done
reset_flags; REVERT_SERVICE_USER=1; REAL_USER="$ME"; REAL_HOME="$DESK"
OUT="$(resolve_service_account "$CHOICE" "$ROOT" 1 2>&1)" || fail "4: the revert failed: $OUT"
[[ "$REAL_USER" == "$ME" && "$REAL_HOME" == "$DESK" ]] || fail "4: after the revert REAL_USER is $REAL_USER"
[[ -d "$DESK/.blackbox" && ! -L "$DESK/.blackbox" && ! -e "$SVC_HOME/.blackbox" ]] || fail "4: .blackbox did not come back"
[[ -f "$DESK/.local/share/zellij/tokens.db" && -d "$DESK/agent-workspaces/t1" ]] || fail "4: the state did not come back"
getfacl -cpn "$DESK" | grep -q "^user:$SVCUID:" && fail "4: the traverse ACL is still there"
getfacl -cpn "$WORK" | grep -q "^user:$SVCUID:" && fail "4: an ancestor's traverse ACL is still there"
[[ ! -e "$CHOICE" ]] || fail "4: the recorded choice was not removed"
[[ ! -e "$DESK/.config/autostart/blackbox-service-display.desktop" ]] || fail "4: the display entry was not removed"
grep -qF "sudo userdel $SVC" <<< "$OUT" || fail "4: the revert does not say how to remove the account"
OUT="$(reset_flags; REVERT_SERVICE_USER=1; resolve_service_account "$CHOICE" "$ROOT" 1 2>&1)" || fail "4: a second revert failed"
grep -q "nothing to revert" <<< "$OUT" || fail "4: a second revert is not a no-op: $OUT"

# ── 6b. renders: unchanged without a service account, retargeted with one ───
RENDER="$WORK/render.sh"
for fn in render_blackbox_unit render_blackbox_mcp_unit render_cli_agent_dropin publish_template_store; do
    sed -n "/^$fn()[[:space:]]*{/,/^}/p" "$INSTALL_SH" >> "$RENDER"
done
# shellcheck source=/dev/null
source "$RENDER"
BLACKBOX_ROOT="$ROOT"
DROPIN_PLAIN="$(unset DESKTOP_USER; REAL_USER="$ME"; REAL_HOME="$DESK"; render_cli_agent_dropin)"
DROPIN_SAME="$(DESKTOP_USER="$ME"; REAL_USER="$ME"; REAL_HOME="$DESK"; render_cli_agent_dropin)"
[[ "$DROPIN_PLAIN" == "$DROPIN_SAME" ]] || fail "6: the drop-in changes without a service account"
grep -qE "^ReadWritePaths=.* /tmp$" <<< "$DROPIN_PLAIN" || fail "6: the default drop-in's ReadWritePaths changed"
DROPIN_SVC="$(DESKTOP_USER="$ME"; REAL_USER="$SVC"; REAL_HOME="$SVC_HOME"; render_cli_agent_dropin)"
grep -qE "^ReadWritePaths=$SVC_HOME/\.claude .* /tmp $SVC_HOME$" <<< "$DROPIN_SVC" \
    || fail "6: the service account's drop-in does not make its home writable: $(grep ReadWritePaths <<< "$DROPIN_SVC")"
grep -q "$DESK" <<< "$DROPIN_SVC" && fail "6: the service account's drop-in still names the desktop home"
for fn in render_blackbox_unit render_blackbox_mcp_unit; do
    grep -qx "User=$SVC" <<< "$(REAL_USER="$SVC"; "$fn")" || fail "6: $fn does not render User=$SVC"
    grep -qx "User=$ME" <<< "$(REAL_USER="$ME"; "$fn")" || fail "6: $fn does not render User=$ME by default"
done
STORE="$WORK/store/etc/blackbox"
( REAL_USER="$SVC"; BLACKBOX_ROOT="$REPO_ROOT"; ETC_BLACKBOX_DIR="$STORE"; INSTALL_SUDO=""
  INSTALL_OWNER="$ME"; INSTALL_GROUP="$(id -gn)"; publish_template_store > /dev/null ) \
    || fail "6: publish_template_store failed for the service account"
grep -qE "^$SVC ALL=\(root\) NOPASSWD: /usr/bin/systemctl restart blackbox.service$" "$STORE/templates/sudoers-system" \
    || fail "6: the sudoers grants are not re-targeted at $SVC"
grep -qE "^$ME " "$STORE/templates/sudoers-system" && fail "6: the sudoers still grants the desktop user"
grep -qx "User=$SVC" "$STORE/templates/zellij-web-unit" || fail "6: zellij-web.service does not run as $SVC"

# ── 7. source-level: desktop steps, no sudo membership, order ────────────────
for anchor in 'blackbox-cu-resolution.desktop"$' 'blackbox-setup.desktop"$' \
              'update-desktop-database "\$DESKTOP_HOME/'; do
    grep -qE "$anchor" "$INSTALL_SH" || fail "7: a desktop-session step lost its anchor: $anchor"
done
grep -nE '^sudo -u "\$REAL_USER" (mkdir|cp|update-desktop-database) .*(autostart|applications)' "$INSTALL_SH" \
    && fail "7: a desktop-session file is written as the service account (above)"
[[ "$(grep -cE '^sudo -u "\$DESKTOP_USER" bash -c' "$INSTALL_SH")" -eq 2 ]] \
    || fail "7: the GNOME and default-browser steps must run as DESKTOP_USER"
grep -nE '(usermod|gpasswd|adduser)[^#]*\b(sudo|admin|wheel)\b' "$INSTALL_SH" | grep -v '^[0-9]*:[[:space:]]*#' \
    && fail "7: install.sh adds an account to an admin group (above)"
# Every refusal comes before the first change (the account is the first one).
MIGRATE="$(sed -n '/^migrate_to_service_account()[[:space:]]*{/,/^}/p' "$INSTALL_SH")"
FIRST_CHANGE="$(grep -n '_svc_run' <<< "$MIGRATE" | head -n1 | cut -d: -f1)"
LAST_REFUSAL="$(grep -n '_svc_refuse' <<< "$MIGRATE" | tail -n1 | cut -d: -f1)"
[[ -n "$FIRST_CHANGE" && -n "$LAST_REFUSAL" && "$LAST_REFUSAL" -lt "$FIRST_CHANGE" ]] \
    || fail "7: migrate_to_service_account can refuse (line $LAST_REFUSAL) after it changed something (line $FIRST_CHANGE)"
S0S_AT="$(grep -n '^resolve_service_account /etc/blackbox/service-user' "$INSTALL_SH" | cut -d: -f1)"
GATE_AT="$(grep -nE '^if \[\[ ! "\$REAL_HOME" =~ \$_root_ok_re \]\]; then' "$INSTALL_SH" | cut -d: -f1)"
BOOT_AT="$(grep -n '^# ── Step 0a: git-clone bootstrap' "$INSTALL_SH" | cut -d: -f1)"
[[ -n "$S0S_AT" && "$GATE_AT" -lt "$S0S_AT" && "$S0S_AT" -lt "$BOOT_AT" ]] \
    || fail "7: Step 0s must run after the home charset gate and before Step 0a's bootstrap"

echo "ALL TESTS PASSED"
exit 0
