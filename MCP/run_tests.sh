#!/usr/bin/env bash
# Run the whole MCP test suite (audit O20-mcp-server-20): pytest over the
# pytest-style MCP/test_*.py files, then every script-style harness listed in
# MCP/conftest.py (SCRIPT_HARNESSES), each as `python <harness>`. Fails if any
# of them fails. The CI mcp job runs exactly this.
#
#   MCP/run_tests.sh [python]
#   MCP_PYTHON=/path/to/python MCP/run_tests.sh
#
# The interpreter defaults to MCP/venv/bin/python, the lean venv the server
# runs in; it needs MCP/requirements.txt plus requirements.dev.txt (pytest).
#
# Hermetic: the harnesses' backend round-trips (GET /operators) go to
# MCP/_stub_backend.py on an ephemeral loopback port, never to the Orchestrator
# on :9091, and credential-shaped variables are dropped from the environment.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
PY="${1:-${MCP_PYTHON:-$here/venv/bin/python}}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "python interpreter not found: $PY" >&2
    exit 2
fi

for name in $(compgen -e); do
    case "$name" in
        *_API_KEY|*_SECRET|*_SECRET_KEY|*_PASSWORD|*_TOKEN|*_ACCOUNT_SID|*_PIN_CODE|GOOGLE_APPLICATION_CREDENTIALS)
            unset "$name" ;;
    esac
done
export BLACKBOX_ROOT="$repo"
# The MCP control listener (POST /internal/reload) of every server a harness
# starts binds a free port, never this box's 9094, and its per-boot reload
# secret goes to a temp dir, not the checkout's credentials/.
export BLACKBOX_MCP_CONTROL_PORT=0
secret_dir="$(mktemp -d)"
export BLACKBOX_MCP_RELOAD_SECRET_FILE="$secret_dir/mcp_reload.secret"

stub_out="$(mktemp)"
"$PY" "$here/_stub_backend.py" >"$stub_out" 2>&1 &
stub_pid=$!
trap 'kill "$stub_pid" 2>/dev/null; rm -f "$stub_out"; rm -rf "$secret_dir"' EXIT
port=""
for _ in $(seq 100); do
    port="$(head -n 1 "$stub_out" 2>/dev/null)"
    [ -n "$port" ] && break
    sleep 0.1
done
if [ -z "$port" ]; then
    echo "the stub backend did not start:" >&2
    cat "$stub_out" >&2
    exit 2
fi
export BLACKBOX_URL="http://127.0.0.1:$port"

mapfile -t harnesses < <("$PY" -c \
    'import runpy, sys; print("\n".join(runpy.run_path(sys.argv[1])["SCRIPT_HARNESSES"]))' \
    "$here/conftest.py")
if [ "${#harnesses[@]}" -eq 0 ]; then
    echo "no script harnesses listed in MCP/conftest.py" >&2
    exit 2
fi

failed=()
echo "=== pytest MCP/ (pytest-style files)"
if ! (cd "$repo" && "$PY" -m pytest -q -p no:cacheprovider MCP); then
    failed+=("pytest MCP/")
fi
for harness in "${harnesses[@]}"; do
    echo
    echo "=== $harness"
    if ! (cd "$here" && "$PY" "$harness"); then
        failed+=("$harness")
    fi
done

echo
if [ "${#failed[@]}" -gt 0 ]; then
    printf 'MCP tests FAILED: %s\n' "${failed[@]}"
    exit 1
fi
echo "MCP tests passed: pytest + ${#harnesses[@]} script harnesses"
