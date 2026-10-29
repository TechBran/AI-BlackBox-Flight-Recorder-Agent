"""Dispatch-routing guard tests for the multi-provider web search migration.

The generic ``web_search`` ToolVault tool was replaced by six per-provider tools
(perplexity/openai/gemini/grok web + grok X + duckduckgo). Every chat/voice/CU
tool-dispatch site must now route those new tool names to
``BlackBoxToolExecutor.execute`` (which resolves them to the on-disk module
executors) instead of a hard-coded ``web_search`` branch or an "Unknown tool"
fallthrough.

These tests are intentionally light on importing the route handlers (they are
WebSocket/streaming generators that are awkward to unit-test in isolation):

1. A source-level guard asserts that NONE of the migrated dispatch files still
   contain the literal ``== "web_search"`` -- proving the old branches are gone.
2. A rail test proves a new per-provider tool name routes through
   ``BlackBoxToolExecutor.execute`` (the exact call every migrated catch-all
   makes) to the module executor, with the network mocked.
"""

import asyncio
import re
from pathlib import Path

import pytest

# Repo root: .../blackbox_poc (this file is Orchestrator/tests/<file>).
REPO_ROOT = Path(__file__).resolve().parents[2]

MIGRATED_FILES = [
    "Orchestrator/routes/chat_routes.py",
    "Orchestrator/routes/chat/context.py",
    "Orchestrator/routes/chat/cu.py",
    "Orchestrator/routes/chat/media.py",
    "Orchestrator/routes/chat/nonstream.py",
    "Orchestrator/routes/chat/providers.py",
    "Orchestrator/routes/chat/save.py",
    "Orchestrator/routes/chat/stream.py",
    "Orchestrator/providers/tool_dispatch.py",
    "Orchestrator/routes/gemini_live_routes.py",
    "Orchestrator/routes/realtime_routes.py",
    "Orchestrator/routes/grok_live_routes.py",
    "Orchestrator/voice/tools.py",  # the tool ladder the three voice routes share
    "Orchestrator/browser/driver_anthropic.py",
]

# Matches `== "web_search"` with any whitespace around the operator.
_WEB_SEARCH_BRANCH = re.compile(r'==\s*"web_search"')


@pytest.mark.parametrize("rel", MIGRATED_FILES)
def test_no_web_search_branch_remains(rel):
    """No migrated dispatch file may still branch on the old generic tool name."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert not _WEB_SEARCH_BRANCH.search(text), (
        f'{rel} still contains a `== "web_search"` branch; the migration must '
        f"route the per-provider web tools through the ToolVault catch-all."
    )


@pytest.mark.parametrize(
    "tool_name,provider",
    [
        ("perplexity_web_search", "perplexity"),
        ("openai_web_search", "openai"),
        ("gemini_web_search", "gemini"),
        ("grok_web_search", "grok"),
        ("grok_x_search", "grok_x"),
        ("duckduckgo_web_search", "duckduckgo"),
    ],
)
def test_new_tool_routes_through_executor(monkeypatch, tool_name, provider):
    """Each new per-provider tool name dispatches through BlackBoxToolExecutor
    to its module executor (the call every migrated catch-all makes)."""
    from Orchestrator.toolvault import registry
    import Orchestrator.web_tools as web_tools
    from Orchestrator.toolvault.context import ToolResult
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

    registry.invalidate_cache()

    captured = {}

    def _fake_search(prov, query, search_recency_filter="month"):
        captured["provider"] = prov
        captured["query"] = query
        return f"RESULTS for {query}"

    monkeypatch.setattr(web_tools, "perform_provider_search", _fake_search)

    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute(tool_name, {"query": "tracked robot"}))

    assert isinstance(result, ToolResult)
    assert result.success is True, f"{tool_name} dispatch failed: {result.result}"
    assert "RESULTS for tracked robot" in result.result
    assert captured["provider"] == provider


# =============================================================================
# Phone-bridge catch-all guard
# =============================================================================
#
# The phone voice bridge used to carry its own tool dispatcher
# (PhoneAIBridge._execute_tool), which hard-``return f"Unknown tool: {name}"``-ed
# any name not in its ``unified_tool_map`` -- and the six per-provider web tools
# were NOT in that map, so phone web search silently broke. The phone's call
# legs now run on the routes' shared LiveVoiceBridge, so their tool calls go
# through the one voice tool ladder (Orchestrator/voice/tools.py), whose
# catch-all routes any unmapped ToolVault tool through the executor by its own
# name.


def test_phone_bridge_has_no_tool_dispatcher_of_its_own():
    """Source-level guard: no second tool map, no hard "Unknown tool"."""
    text = (REPO_ROOT / "Orchestrator/phone/bridge.py").read_text(encoding="utf-8")
    assert '"web_search": "web_search"' not in text, (
        "phone bridge still maps the deleted generic web_search tool."
    )
    assert 'return f"Unknown tool: {name}"' not in text, (
        "phone bridge still hard-returns 'Unknown tool' instead of a catch-all."
    )
    assert "unified_tool_map" not in text and "def _execute_tool" not in text, (
        "phone bridge grew its own tool dispatcher again; its legs answer tool "
        "calls through the shared voice tool ladder."
    )


def test_voice_ladder_routes_unmapped_web_tool_through_executor(monkeypatch):
    """The voice tool ladder (every live-voice session, phone legs included)
    passes an UNMAPPED web tool name (e.g. ``gemini_web_search``) straight
    through to the unified executor by its own name -- this is what makes
    phone web search work."""
    import Orchestrator.tools as tools_pkg
    from Orchestrator.toolvault.context import ToolResult
    from Orchestrator.voice import tools as voice_tools

    captured = {}

    class _FakeExecutor:
        def __init__(self, operator="system", **kw):
            captured["operator"] = operator

        async def execute(self, tool_name, tool_input):
            captured["tool_name"] = tool_name
            captured["input"] = tool_input
            return ToolResult(success=True, result="EXECUTOR_RAN")

    # The ladder imports BlackBoxToolExecutor from this package at call time.
    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _FakeExecutor)

    session = type("S", (), {"operator": "Brandon", "portal_ws": None})()
    out = asyncio.run(voice_tools.execute_voice_tool(
        session, "gemini_web_search", {"query": "tracked robot"},
        log_prefix="[PHONE-TEST]", created_by="phone-test"))

    assert out == "EXECUTOR_RAN"
    # Unmapped name routed through by its OWN name (catch-all), not dropped.
    assert captured["tool_name"] == "gemini_web_search"
    assert captured["operator"] == "Brandon"
    assert captured["input"] == {"query": "tracked robot"}
