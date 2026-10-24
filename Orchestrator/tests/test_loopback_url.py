"""Orchestrator.utils.loopback must work where fastapi does not exist.

The BlackBox MCP server runs the tool executor in its own lean venv (MCP/venv),
and a module-level fastapi import there means ZERO tools, silently. M3 put the
loopback-URL helper into config.py, which imports fastapi; the MCP suite caught
it. This pins the stdlib-only path and that config.py still agrees with it.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_tool_executor_imports_without_fastapi(tmp_path):
    code = (
        "import sys; sys.modules['fastapi'] = None\n"  # makes `import fastapi` raise
        "from Orchestrator.utils.loopback import orchestrator_url, orchestrator_port\n"
        "from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor\n"
        "print(orchestrator_url('/chat/save'))\n"
        "print(BlackBoxToolExecutor(operator='x').base_url)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                       env={"PATH": "", "ORCHESTRATOR_PORT": "9777", "PYTHONPATH": str(REPO)},
                       timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.splitlines() == ["http://localhost:9777/chat/save", "http://localhost:9777"]


def test_config_and_loopback_agree(monkeypatch):
    """With config loaded, its ORCHESTRATOR_PORT is the one truth for both
    (tests patch it; the env var only matters where config never loads, which
    the subprocess test above covers)."""
    from Orchestrator import config
    from Orchestrator.utils import loopback
    assert config.orchestrator_url("/x", scheme="ws") == f"ws://localhost:{config.ORCHESTRATOR_PORT}/x"
    assert loopback.orchestrator_url("/x") == config.orchestrator_url("/x")
    monkeypatch.setenv("ORCHESTRATOR_PORT", "9555")   # ignored while config is loaded
    monkeypatch.setattr(config, "ORCHESTRATOR_PORT", 9777)
    assert loopback.orchestrator_url("/y") == "http://localhost:9777/y"
    assert config.orchestrator_url("/y") == "http://localhost:9777/y"
