"""P1b tripwire: voice prompts must only mandate tools the declared groups contain."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROUTES = [
    REPO / "Orchestrator/routes/realtime_routes.py",
    REPO / "Orchestrator/routes/grok_live_routes.py",
    REPO / "Orchestrator/routes/gemini_live_routes.py",
]


def test_no_stale_get_recent_snapshots_references():
    for path in ROUTES:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "get_recent_snapshots" in line:
                assert "list_recent_snapshots" in line, (
                    f"{path.name}:{i} references get_recent_snapshots — NOT a declared "
                    f"ToolVault tool (models can only call declared functions): {line.strip()}"
                )


def test_declared_groups_carry_list_recent_snapshots():
    from Orchestrator.tools.tool_registry import (
        get_gemini_live_tools,
        get_openai_realtime_tools,
    )
    for group in ("realtime", "grok_live"):
        names = [t["name"] for t in get_openai_realtime_tools(group)]
        assert "list_recent_snapshots" in names
        assert "get_recent_snapshots" not in names
    assert "list_recent_snapshots" in json.dumps(get_gemini_live_tools("gemini_live"))
