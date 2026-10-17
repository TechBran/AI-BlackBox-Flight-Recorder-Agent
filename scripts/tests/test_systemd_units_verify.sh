#!/usr/bin/env bash
# Renders every systemd unit and drop-in the installer writes, for a sample
# install root, and checks them the way systemd will read them.
#
# 2026-10-09 audit W39 / O22-01 / X04-security-clients-infra-14 (rank 40):
#   - the time-sync drop-in said `Wants=systemd-timesyncd`; systemd rejects a
#     dependency without a unit suffix ("Failed to add dependency ... Invalid
#     argument"), so the boot gate was a silent no-op on every box;
#   - BLACKBOX_ROOT was allowed to contain spaces, `&`, `()` and `%`, which the
#     unquoted ExecStart=/WorkingDirectory=/ReadWritePaths= values split on or
#     expand as specifiers (226/NAMESPACE, 203/EXEC);
#   - unescaped backticks in the blackbox.service heredoc ran as command
#     substitutions at install time and deleted text from the deployed unit.
#
# Units come from the real render code: the render_* functions in install.sh
# and in the GPU/localstack installer scripts (extracted via sed — none of these
# scripts has a main() guard), zellij-web.service via the same sed as Step 2c,
# and the Asterisk drop-in by running setup_asterisk_blackbox_include() in a
# sandbox. The sample root uses every punctuation character the root charset
# allows. Static checks always run; `systemd-analyze verify` runs where it
# exists (skipped with a note otherwise), against SYSTEMD_UNIT_PATH limited to
# the sandbox plus the distro unit dirs, so the host's own /etc units (a real
# blackbox.service and its drop-ins on a BlackBox) never leak into the result.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/Scripts/install.sh"
RERANKER_SH="$REPO_ROOT/installer/templates/blackbox-install-reranker.sh"
LOCALSTACK_SH="$REPO_ROOT/installer/templates/blackbox-install-localstack.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

for f in "$INSTALL_SH" "$RERANKER_SH" "$LOCALSTACK_SH"; do
    [[ -f "$f" ]] || fail "missing $f"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

extract() {  # <script> <function> — append the function's source to $FUNC_FILE
    sed -n "/^$2()[[:space:]]*{/,/^}/p" "$1" >> "$FUNC_FILE"
    grep -q "^$2()" "$FUNC_FILE" || fail "could not extract $2() from $1"
}
FUNC_FILE="$WORK/func.sh"
for fn in render_blackbox_unit render_blackbox_mcp_unit render_override_dropin \
          render_cli_agent_dropin render_time_sync_dropin render_ydotoold_unit \
          setup_asterisk_blackbox_include; do
    extract "$INSTALL_SH" "$fn"
done
extract "$RERANKER_SH" render_vllm_reranker_unit
extract "$LOCALSTACK_SH" render_models_unit
# shellcheck source=/dev/null
source "$FUNC_FILE"

# ── sample install: root + home with every allowed punctuation character ──────
# Globals the sourced render_* functions read.
BLACKBOX_ROOT="$WORK/srv/bb-root_v2.0+x@y,z~/blackbox_poc."
REAL_USER="bbx"
REAL_HOME="$WORK/home/bb.user-1"
# shellcheck disable=SC2034
USER_UID=1000
# shellcheck disable=SC2034
USER_GID=1000
LOCALSTACK_HOME="$REAL_HOME/.blackbox/localstack"
LOCALSTACK_BIN="$LOCALSTACK_HOME/bin"
TEMPLATE_DIR="$REPO_ROOT/installer/templates"
# shellcheck disable=SC2034
UNIT_TEMPLATE="$TEMPLATE_DIR/vllm-reranker.service"
# The root must pass install.sh's own gate, or this test proves nothing.
ROOT_RE_LINE="$(grep -E '^_root_ok_re=' "$INSTALL_SH" | head -n 1)"
[[ -n "$ROOT_RE_LINE" ]] || fail "install.sh no longer defines _root_ok_re"
eval "$ROOT_RE_LINE"
# shellcheck disable=SC2154  # assigned by the eval above
[[ "$BLACKBOX_ROOT" =~ $_root_ok_re ]] || fail "sample root '$BLACKBOX_ROOT' is rejected by install.sh's own charset"
[[ "$REAL_HOME" =~ $_root_ok_re ]] || fail "sample home '$REAL_HOME' is rejected by install.sh's own charset"

# Executables the units start. Under the sample root/home they are stubbed;
# system binaries we do not ship are stubbed only when the host lacks them.
for exe in "$BLACKBOX_ROOT/Orchestrator/venv/bin/python" "$BLACKBOX_ROOT/MCP/venv/bin/python" \
           "$REAL_HOME/start-reranker.sh" "$LOCALSTACK_BIN/llama-swap"; do
    mkdir -p "$(dirname "$exe")"; printf '#!/bin/sh\n' > "$exe"; chmod +x "$exe"
done

UNITS="$WORK/units"
mkdir -p "$UNITS/blackbox.service.d"
render_into() {  # <dest> <render-fn> — render, failing on any stderr output
    "$2" > "$UNITS/$1" 2> "$WORK/render.err" || fail "$2 exited non-zero: $(cat "$WORK/render.err")"
    [[ -s "$WORK/render.err" ]] && fail "$2 wrote to stderr while rendering (a heredoc expansion ran something): $(cat "$WORK/render.err")"
    return 0
}
render_into blackbox.service                         render_blackbox_unit
render_into blackbox-mcp.service                     render_blackbox_mcp_unit
render_into blackbox.service.d/override.conf         render_override_dropin
render_into blackbox.service.d/cli-agent-overrides.conf render_cli_agent_dropin
render_into blackbox.service.d/time-sync.conf        render_time_sync_dropin
render_into ydotoold.service                         render_ydotoold_unit
render_into vllm-reranker.service                    render_vllm_reranker_unit
render_into blackbox-models.service                  render_models_unit
sed "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" "$TEMPLATE_DIR/zellij-web.service" > "$UNITS/zellij-web.service"
ASTERISK_ETC="$WORK/asterisk" BLACKBOX_D="$WORK/asterisk/blackbox.d" \
SYSTEMD_DROPIN_DIR="$UNITS/blackbox.service.d" SUDOERS_FILE="$WORK/blackbox-asterisk" \
SERVICE_USER="$(id -un)" SKIP_VISUDO=1 \
    setup_asterisk_blackbox_include > "$WORK/asterisk.log" 2>&1 \
    || fail "setup_asterisk_blackbox_include failed in the sandbox: $(cat "$WORK/asterisk.log")"
[[ -f "$UNITS/blackbox.service.d/asterisk.conf" ]] || fail "asterisk drop-in not rendered"

mapfile -t RENDERED < <(find "$UNITS" -type f | sort)
[[ "${#RENDERED[@]}" -eq 10 ]] || fail "expected 10 rendered units/drop-ins, got ${#RENDERED[@]}"

# ── static checks (no systemd needed) ─────────────────────────────────────────
DEP_RE='^(Wants|Requires|Requisite|BindsTo|PartOf|Upholds|Conflicts|Before|After|OnFailure|OnSuccess|WantedBy|RequiredBy|UpheldBy|Also)='
SUFFIX_RE='\.(service|socket|target|device|mount|automount|swap|timer|path|slice|scope)$'
for f in "${RENDERED[@]}"; do
    rel="${f#"$UNITS"/}"
    grep -q 'PLACEHOLDER' "$f" && fail "$rel: a PLACEHOLDER survived rendering"
    while IFS= read -r line; do
        for dep in ${line#*=}; do
            [[ "$dep" =~ $SUFFIX_RE ]] \
                || fail "$rel: dependency '$dep' has no unit suffix — systemd ignores it ('$line')"
        done
    done < <(grep -E "$DEP_RE" "$f")
    # `%<letter>` is a specifier in almost every unit setting; none of ours uses
    # one (a bare `%`, as in MemoryHigh=70%, is not one).
    grep -vE '^[[:space:]]*(#|;|$)' "$f" | grep -qE '%[A-Za-z%]' \
        && fail "$rel: a non-comment line contains a %-specifier (systemd expands it)"
done
grep -qx 'Wants=systemd-timesyncd.service' "$UNITS/blackbox.service.d/time-sync.conf" \
    || fail "time-sync drop-in no longer pulls in systemd-timesyncd.service"
grep -qx 'After=time-sync.target' "$UNITS/blackbox.service.d/time-sync.conf" \
    || fail "time-sync drop-in no longer orders after time-sync.target"
# The perimeter comment quotes a sudoers grant in backticks; it must reach the
# deployed unit verbatim, not be executed while the heredoc expands.
grep -qF '`NOPASSWD: /usr/local/sbin/blackbox-apt-install <pkg>`' "$UNITS/blackbox.service" \
    || fail "blackbox.service lost the backtick-quoted perimeter comment (unescaped backticks in the heredoc)"
grep -qx "ReadWritePaths=$BLACKBOX_ROOT" "$UNITS/blackbox.service" \
    || fail "blackbox.service ReadWritePaths is not the bare install root"
grep -qx "ExecStart=$BLACKBOX_ROOT/MCP/venv/bin/python $BLACKBOX_ROOT/MCP/blackbox_mcp_server.py --transport http" \
    "$UNITS/blackbox-mcp.service" || fail "blackbox-mcp.service ExecStart did not render the sample root"

# ── systemd-analyze verify ────────────────────────────────────────────────────
if ! command -v systemd-analyze >/dev/null 2>&1; then
    echo "SKIP: systemd-analyze not available — static unit checks only"
    echo "ALL TESTS PASSED"
    exit 0
fi

# Stub any system binary a unit starts that this host does not have (e.g.
# /usr/local/bin/zellij on a CI runner) — verify checks executability.
mkdir -p "$WORK/sysbin"
for f in "${RENDERED[@]}"; do
    while IFS= read -r exe; do
        exe="${exe#"${exe%%[!@+!:-]*}"}"
        [[ -x "$exe" ]] && continue
        case "$exe" in
            "$BLACKBOX_ROOT"/*|"$REAL_HOME"/*) fail "test bug: $exe under the sample install is not stubbed" ;;
        esac
        stub="$WORK/sysbin/$(basename "$exe")"
        printf '#!/bin/sh\n' > "$stub"; chmod +x "$stub"
        sed -i "s|$exe|$stub|g" "$f"
    done < <(sed -nE 's/^Exec[A-Za-z]*=([^[:space:]]+).*/\1/p' "$f")
done

SYS_DIRS=""
for d in /usr/lib/systemd/system /lib/systemd/system; do
    [[ -d "$d" ]] && SYS_DIRS="$SYS_DIRS:$d"
done
TOP_UNITS=()
for f in "${RENDERED[@]}"; do
    [[ "$f" == *.service && "$(dirname "$f")" == "$UNITS" ]] && TOP_UNITS+=("$f")
done
OUT="$(SYSTEMD_UNIT_PATH="$UNITS$SYS_DIRS" systemd-analyze verify "${TOP_UNITS[@]}" 2>&1)"; RC=$?
# Only diagnostics about OUR files and units count; a distro unit's own noise
# (pulled in through multi-user.target) is not this installer's problem.
OURS_RE="$WORK|^(blackbox|blackbox-mcp|ydotoold|zellij-web|vllm-reranker|blackbox-models)\.service"
MINE="$(grep -E "$OURS_RE" <<< "$OUT" || true)"
if [[ -n "$MINE" ]]; then
    fail "systemd-analyze verify flagged the rendered units:
$MINE"
fi
if [[ "$RC" -ne 0 ]]; then
    echo "NOTE: systemd-analyze verify exited $RC on host units only:" >&2
    echo "$OUT" >&2
fi

echo "ALL TESTS PASSED"
exit 0
