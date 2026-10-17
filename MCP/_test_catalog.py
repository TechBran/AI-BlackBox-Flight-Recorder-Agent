"""Shared by the MCP test harnesses: the size of the MCP tool catalog.

The catalog is derived from the ToolVault modules (every tool whose schema.json
lists the "mcp" group), so the tests track the registry instead of a literal
count that went stale every time a tool was added (74 -> 79 by 2026-10).

The server lists only the tools whose x-availability gate passes: a per-provider
web-search or image tool needs its provider key (the local image tool needs a
custom image server). expected_mcp_tool_count() applies the same gate in this
environment, so a fresh box or a CI runner, which hold no keys, expects the
ungated catalog; all_providers_available() opens every gate for a test that
checks the full catalog. Stdlib only, so it imports in the lean MCP venv.
"""
import contextlib
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TOOLS = _REPO / "ToolVault" / "tools"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def mcp_tool_schemas() -> list:
    out = []
    for schema in sorted(_TOOLS.glob("*/schema.json")):
        try:
            entry = json.loads(schema.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "mcp" in (entry.get("groups") or []):
            out.append(entry)
    return out


def expected_mcp_tool_count(available_only: bool = True) -> int:
    """The tools the server lists here; available_only=False: the full catalog."""
    schemas = mcp_tool_schemas()
    if not available_only:
        return len(schemas)
    from Orchestrator.toolvault import availability
    return len(availability.filter_available(schemas))


@contextlib.contextmanager
def all_providers_available():
    """Every x-availability gate open (fake keys, a local image server)."""
    from Orchestrator.toolvault import availability

    keys = {env for spec in availability.FEATURES.values()
            for env in spec["provider_env"].values() if env}
    saved = availability._read_env, availability._local_image_available
    availability._read_env = lambda: {k: "test-key" for k in keys}
    availability._local_image_available = lambda: True
    try:
        yield
    finally:
        availability._read_env, availability._local_image_available = saved
