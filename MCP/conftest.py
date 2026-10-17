"""MCP/ under pytest: collect the pytest-style test files only.

The harnesses below are scripts (python test_mcp_auth.py ...). Two of them run
their checks at import time, so pytest collecting them would execute them, and
the others drive a server subprocess from main(). MCP/run_tests.sh runs them as
scripts and fails on any non-zero exit; it reads this list (audit
O20-mcp-server-20). Stdlib only: the lean MCP venv has no Orchestrator deps.
"""

SCRIPT_HARNESSES = [
    "test_mcp_auth.py",
    "test_mcp_http_transport.py",
    "test_mcp_oauth.py",
    "test_mcp_operator_scoping.py",
    "test_mcp_runtime_reload.py",
]

collect_ignore = list(SCRIPT_HARNESSES)
