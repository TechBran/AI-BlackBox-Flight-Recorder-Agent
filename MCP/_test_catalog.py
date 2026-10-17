"""Shared by the MCP test harnesses: the size of the MCP tool catalog.

The catalog is derived from the ToolVault modules (every tool whose schema.json
lists the "mcp" group), so the tests track the registry instead of a literal
count that went stale every time a tool was added (74 -> 79 by 2026-10).
Stdlib only, so it imports in the lean MCP venv.
"""
import json
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "ToolVault" / "tools"


def expected_mcp_tool_count() -> int:
    n = 0
    for schema in sorted(_TOOLS.glob("*/schema.json")):
        try:
            groups = json.loads(schema.read_text(encoding="utf-8")).get("groups") or []
        except (OSError, ValueError):
            continue
        if "mcp" in groups:
            n += 1
    return n
