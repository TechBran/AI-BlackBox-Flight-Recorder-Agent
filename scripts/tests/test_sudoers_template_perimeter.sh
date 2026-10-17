#!/usr/bin/env bash
# Perimeter test for installer/templates/sudoers-blackbox-system.
#
# The sudoers file IS the in-service→root perimeter: a prompt-injection RCE in
# the service can run, as root, exactly what these grants match and nothing
# else. Two grant shapes quietly void that (2026-10-09 audit X04-security-
# clients-infra-02 / rank 17):
#   - a trailing `*` (or any glob): sudoers matches the arguments as ONE
#     concatenated string, so `tailscale up --hostname=*` also matched
#     `--hostname=x --login-server=https://evil --ssh`, and `tailscale cert *`
#     matched `--cert-file=/etc/<anything>` (a root file-overwrite primitive);
#   - a command with NO arguments, which sudoers reads as "any arguments".
# A grant on a shell, interpreter or editor is a root shell outright.
#
# This test renders the template exactly as install.sh / publish_template_store
# do, runs `visudo -c -f` on the result, and then walks every grant:
#   1. one recognised `<user> ALL=(root) NOPASSWD: /abs/cmd <args>` per line;
#   2. no grant with bare/empty arguments, no glob in literal arguments, no
#      unanchored or any-character regex;
#   3. no grant on a shell / interpreter / editor / file-writing tool;
#   4. the grant list equals the golden list below (a new grant is a reviewed
#      edit to this file, never a silent template change);
#   5. each anchored regex grant ACCEPTS the argv the service really builds and
#      REJECTS the hostile variants (matched with bash's POSIX ERE, the same
#      regcomp(REG_EXTENDED) engine sudo uses for `^…$` arguments);
#   6. the write-systemd kinds granted are exactly the kinds install.sh
#      publishes to the trusted store, each a real helper case arm;
#   7. the retired journalctl and zellij-binary grants stay gone.
#
# Hermetic: renders into a mktemp dir, never reads or writes /etc/sudoers*.
# A live `sudo -l -U <service-user>` check stays an operator acceptance step
# (it needs root).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMPL="$REPO_ROOT/installer/templates/sudoers-blackbox-system"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
WS="$REPO_ROOT/installer/templates/blackbox-write-systemd.sh"
VISUDO=/usr/sbin/visudo

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$TMPL" ]] || fail "sudoers template not found at $TMPL"
[[ -f "$INSTALL_SH" ]] || fail "install.sh not found at $INSTALL_SH"
[[ -f "$WS" ]] || fail "blackbox-write-systemd.sh not found at $WS"
# sudo is MUST_HAVE (Scripts/onboarding/system-packages.txt), so visudo is
# always present where this template is installed. Its syntax gate is the
# point of this test, so a missing visudo is a failure, not a skip.
[[ -x "$VISUDO" ]] || fail "visudo not found at $VISUDO (the sudo package provides it)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. render the template exactly as install.sh does ─────────────────────────
# Both placeholders, with the same sed escaping as every install.sh
# substitution site. The root carries `&` and a space — the sed-escaping
# incident class — so a broken render cannot hide behind a tidy path.
RENDER_USER="bbx"
RENDER_ROOT="/home/bbx/r&d lab/blackbox-poc"
ROOT_ESC="${RENDER_ROOT//\\/\\\\}"; ROOT_ESC="${ROOT_ESC//&/\\&}"; ROOT_ESC="${ROOT_ESC//|/\\|}"
RENDERED="$WORK/blackbox-system"
sed -e "s|REAL_USER_PLACEHOLDER|$RENDER_USER|g" \
    -e "s|BLACKBOX_ROOT_PLACEHOLDER|$ROOT_ESC|g" \
    "$TMPL" > "$RENDERED"
grep -q 'PLACEHOLDER' "$RENDERED" && fail "a PLACEHOLDER survived the render"

# ── 2. visudo syntax gate ─────────────────────────────────────────────────────
if ! "$VISUDO" -c -f "$RENDERED" >/dev/null 2>&1; then
    "$VISUDO" -c -f "$RENDERED" >&2 || true
    fail "the rendered sudoers template does not pass visudo -c"
fi

# ── 3. walk every grant ───────────────────────────────────────────────────────
# Binaries that are a root shell (or a root write/exec primitive) under ANY
# argument shape. A grant on one of these is never acceptable.
FORBIDDEN_RE='^(bash|sh|dash|zsh|ksh|csh|tcsh|fish|busybox|env|su|sudo|sudoedit|python[0-9.]*|perl[0-9.]*|ruby[0-9.]*|node|nodejs|php[0-9.]*|lua[0-9.]*|tclsh|expect|vi|vim|nvim|view|nano|pico|emacs|ed|ex|less|more|man|tee|cp|mv|dd|install|ln|rsync|chmod|chown|find|xargs|awk|gawk|mawk|sed|journalctl)$'
GRANT_RE="^${RENDER_USER} ALL=\(root\) NOPASSWD: (/[^[:space:]]+)( (.+))?\$"
# An anchored regex is only as tight as its atoms. Reject the ones that can
# match a space (and so a whole extra option): an unescaped any-character '.',
# a negated bracket or POSIX class, a GNU shorthand class, or a bracket
# expression that itself lists a space. Escapes are dropped before bracket
# expressions so `\.` and the `.` inside `[a-z0-9.+-]` are not mistaken for
# the any-character atom.
regex_is_tight() {  # <regex>
    local re="$1" brackets stripped
    case "$re" in *'[^'*|*'[[:'*) return 1 ;; esac
    case "$re" in *'\s'*|*'\S'*|*'\w'*|*'\W'*) return 1 ;; esac
    brackets="$(printf '%s' "$re" | grep -o '\[[^]]*\]' || true)"
    [[ "$brackets" == *' '* ]] && return 1
    stripped="$(printf '%s' "$re" | sed -e 's/\\.//g' -e 's/\[[^]]*\]//g')"
    [[ "$stripped" == *.* ]] && return 1
    return 0
}

GRANTS="$WORK/grants"
: > "$GRANTS"
declare -A REGEX_GRANT=()
nlines=0
while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    # Anything that is not a single-command grant line (Defaults, aliases,
    # (ALL) runas, multi-line continuations) is rejected outright, so no other
    # sudoers syntax can carry a grant past the checks below.
    [[ "$line" =~ $GRANT_RE ]] || fail "unrecognised sudoers line (only '$RENDER_USER ALL=(root) NOPASSWD: /abs/cmd <args>' is allowed): $line"
    cmd="${BASH_REMATCH[1]}"
    args="${BASH_REMATCH[3]:-}"
    nlines=$((nlines + 1))

    case "$cmd" in
        *'*'*|*'?'*|*'['*) fail "a glob in the command path matches more than one binary: $line" ;;
    esac
    base="${cmd##*/}"
    [[ "$base" =~ $FORBIDDEN_RE ]] && fail "grant on a shell/interpreter/editor/write tool ($base) is a root shell: $line"

    if [[ -z "$args" ]]; then
        fail "grant with no arguments — sudoers reads that as ANY arguments: $line"
    elif [[ "$args" == '""' ]]; then
        :   # explicitly no arguments
    elif [[ "$args" == ^* ]]; then
        # sudo >= 1.9.10 treats ^…$ arguments as one POSIX ERE over the whole
        # space-joined argument string (sudoers(5) "Regular expressions").
        [[ "$args" == *\$ ]] || fail "regex grant is not anchored with a trailing \$: $line"
        regex_is_tight "$args" \
            || fail "regex grant can match arbitrary text (any-character '.', a negated or POSIX class, a shorthand class, or a space in a bracket): $line"
        REGEX_GRANT["$cmd ${args%% *}"]="$args"
    else
        # Literal arguments are matched as one string with glob semantics, so
        # ANY glob character (not just a trailing `*`) can match across spaces.
        case "$args" in
            *'*'*|*'?'*|*'['*|*']'*)
                fail "glob in literal arguments matches across word boundaries (use exact args or an anchored ^…\$ regex): $line" ;;
        esac
        # One command per grant keeps every line auditable on its own.
        [[ "$args" == *,* ]] && fail "multiple commands on one grant line: $line"
    fi
    printf '%s %s\n' "$cmd" "$args" >> "$GRANTS"
done < "$RENDERED"
[[ "$nlines" -gt 0 ]] || fail "no grants parsed from the rendered template"

# ── 4. golden grant list ──────────────────────────────────────────────────────
# Every grant, in file order. Changing the perimeter means changing this list
# in the same reviewed commit.
GOLDEN="$WORK/golden"
cat > "$GOLDEN" <<'GOLDEN_EOF'
/usr/bin/apt-get install -y tailscale
/usr/bin/tailscale ^up --accept-dns=true --hostname=[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$
/usr/bin/tailscale set --accept-dns=true
/usr/bin/tailscale ^cert [a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net$
/usr/bin/tailscale serve --bg --https=443 9091
/usr/bin/tailscale serve status --json
/usr/bin/tailscale serve reset
/usr/bin/tailscale logout
/usr/bin/systemctl start tailscaled
/usr/bin/systemctl enable tailscaled
/usr/bin/systemctl restart blackbox.service
/usr/bin/tailscale funnel --bg --https=8443 9093
/usr/bin/tailscale funnel status --json
/usr/bin/tailscale funnel reset
/usr/bin/systemctl start blackbox-mcp.service
/usr/bin/systemctl restart blackbox-mcp.service
/usr/bin/systemctl stop blackbox-mcp.service
/usr/local/sbin/blackbox-apt-install ^[a-z0-9][a-z0-9.+-]*$
/usr/local/sbin/blackbox-write-systemd sudoers-system
/usr/local/sbin/blackbox-write-systemd apt-install-helper
/usr/local/sbin/blackbox-write-systemd write-systemd-helper
/usr/local/sbin/blackbox-write-systemd zellij-web-unit
/usr/bin/systemctl restart zellij-web.service
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl restart blackbox-models.service
/usr/bin/systemctl stop blackbox-models.service
/usr/bin/systemctl disable --now vllm-reranker.service
/usr/bin/systemctl stop vllm-reranker.service
/usr/bin/systemctl stop ollama.service
GOLDEN_EOF
if ! diff -u "$GOLDEN" "$GRANTS" >"$WORK/golden.diff"; then
    cat "$WORK/golden.diff" >&2
    fail "the sudoers grant list drifted from the golden list (update both in one reviewed commit)"
fi

# ── 5. anchored regex grants: accept the real argv, reject the hostile ones ──
# The strings are what sudo matches: the arguments joined by single spaces.
expect_match() {  # <grant-key> <arg-string>
    local re="${REGEX_GRANT[$1]:-}"
    [[ -n "$re" ]] || fail "no regex grant for '$1'"
    [[ "$2" =~ $re ]] || fail "'$1' grant REJECTS a legitimate call: [$2]"
}
expect_reject() {  # <grant-key> <arg-string>
    local re="${REGEX_GRANT[$1]:-}"
    [[ -n "$re" ]] || fail "no regex grant for '$1'"
    [[ "$2" =~ $re ]] && fail "'$1' grant ACCEPTS a hostile call: [$2]"
    return 0
}

UP="/usr/bin/tailscale ^up"
# tailscale_actuator.start_up(): `up --accept-dns=true --hostname=<_safe_hostname()>`
expect_match  "$UP" "up --accept-dns=true --hostname=ai-black-box-fc-a620ai-wifi"
expect_match  "$UP" "up --accept-dns=true --hostname=blackbox"
expect_match  "$UP" "up --accept-dns=true --hostname=a"
expect_match  "$UP" "up --accept-dns=true --hostname=$(printf 'a%.0s' {1..63})"
expect_reject "$UP" "up --accept-dns=true --hostname=box --login-server=https://evil.example"
expect_reject "$UP" "up --accept-dns=true --hostname=box --ssh"
expect_reject "$UP" "up --accept-dns=true --hostname=box --auth-key=tskey-auth-x"
expect_reject "$UP" "up --accept-dns=true --hostname=box --reset"
expect_reject "$UP" "up --accept-dns=true --hostname=box --advertise-exit-node"
expect_reject "$UP" "up --accept-dns=true --hostname=box  --ssh"
expect_reject "$UP" "up --accept-dns=true --hostname= --ssh"
expect_reject "$UP" "up --accept-dns=true --hostname="
expect_reject "$UP" "up --accept-dns=true --hostname=-box"
expect_reject "$UP" "up --accept-dns=true --hostname=box-"
expect_reject "$UP" "up --accept-dns=true --hostname=Box"
expect_reject "$UP" "up --accept-dns=true --hostname=$(printf 'a%.0s' {1..64})"
expect_reject "$UP" "up --hostname=box --accept-dns=true"
expect_reject "$UP" "up --login-server=https://evil.example --accept-dns=true --hostname=box"
expect_reject "$UP" "up --accept-dns=true --hostname=box"$'\n'"--ssh"

CERT="/usr/bin/tailscale ^cert"
# tailscale_actuator.request_cert(): `cert <Self.DNSName without the trailing dot>`
expect_match  "$CERT" "cert ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net"
expect_match  "$CERT" "cert box-1.tail-scale.ts.net"
expect_reject "$CERT" "cert --cert-file=/etc/sudoers.d/zz box.tail1.ts.net"
expect_reject "$CERT" "cert box.tail1.ts.net --cert-file=/etc/shadow"
expect_reject "$CERT" "cert box.tail1.ts.net --key-file=/root/.ssh/authorized_keys"
expect_reject "$CERT" "cert --cert-file=- box.tail1.ts.net"
expect_reject "$CERT" "cert /etc/shadow"
expect_reject "$CERT" "cert box.tail1.ts.net.evil.example"
expect_reject "$CERT" "cert box.tail1.ts.net "
expect_reject "$CERT" "cert -box.tail1.ts.net"
expect_reject "$CERT" "cert ../../etc/x.ts.net"
expect_reject "$CERT" "cert box..ts.net"
expect_reject "$CERT" "cert"

APT="/usr/local/sbin/blackbox-apt-install ^[a-z0-9][a-z0-9.+-]*$"
# runner.py: `blackbox-apt-install <pkg>` with one allowlisted package, and the
# preflight probe package name.
expect_match  "$APT" "curl"
expect_match  "$APT" "python3.12"
expect_match  "$APT" "g++"
expect_match  "$APT" "libevdev-dev"
expect_match  "$APT" "blackbox-preflight-probe-not-a-real-package"
expect_reject "$APT" "curl wget"
expect_reject "$APT" "-o APT::Get::AllowUnauthenticated=true curl"
expect_reject "$APT" "--allow-unauthenticated"
expect_reject "$APT" "curl;id"
expect_reject "$APT" "Curl"
expect_reject "$APT" ""

# ── 6. write-systemd kinds granted == kinds install.sh publishes ──────────────
# A kind that is granted but never published fails closed (exit 6) — dead
# weight in the perimeter. A kind that is published but not granted can never
# be drift-healed. Keep the two lists identical, and every kind a real arm.
GRANTED_KINDS="$(awk '$1 == "/usr/local/sbin/blackbox-write-systemd" {print $2}' "$GRANTS" | sort)"
PUBLISHED_KINDS="$(sed -n "/done <<'STORE_MANIFEST'/,/^STORE_MANIFEST\$/p" "$INSTALL_SH" \
    | grep -vE "STORE_MANIFEST|^[[:space:]]*\$" | awk '{print $1}' | sort)"
[[ -n "$PUBLISHED_KINDS" ]] || fail "could not read publish_template_store's STORE_MANIFEST from install.sh"
[[ "$GRANTED_KINDS" == "$PUBLISHED_KINDS" ]] \
    || fail "granted write-systemd kinds [$(echo $GRANTED_KINDS)] != published store kinds [$(echo $PUBLISHED_KINDS)]"
for kind in $GRANTED_KINDS; do
    grep -qE "^[[:space:]]*${kind}\)" "$WS" \
        || fail "granted write-systemd kind '$kind' has no case arm in the helper"
done

# ── 7. retired grants stay retired ────────────────────────────────────────────
# journalctl: the service reads its journal as a member of systemd-journal
# (install.sh adds it), never as root. The zellij binary helper trusted a
# service-writable pin file and is deleted along with its grant.
grep -qE '^[^#]*NOPASSWD:.*journalctl' "$RENDERED" \
    && fail "a journalctl grant is back — journal access is the systemd-journal group, not root"
grep -qE '^[^#]*blackbox-install-zellij-binary' "$RENDERED" \
    && fail "the zellij binary helper grant is back"
[[ -e "$REPO_ROOT/installer/templates/blackbox-install-zellij-binary.sh" ]] \
    && fail "installer/templates/blackbox-install-zellij-binary.sh still exists"
grep -nE '^[^#]*blackbox-install-zellij-binary\.sh' "$INSTALL_SH" \
    && fail "install.sh still installs the zellij binary helper"
grep -qE '^[^#]*rm -f[^#]*/usr/local/sbin/blackbox-install-zellij-binary' "$INSTALL_SH" \
    || fail "install.sh does not remove a previously deployed /usr/local/sbin/blackbox-install-zellij-binary"

echo "ALL TESTS PASSED"
exit 0
