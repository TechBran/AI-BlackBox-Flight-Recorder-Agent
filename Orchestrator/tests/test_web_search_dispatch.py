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
    "Orchestrator/routes/gemini_live_routes.py",
    "Orchestrator/routes/realtime_routes.py",
    "Orchestrator/routes/grok_live_routes.py",
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
# The phone voice bridge (Orchestrator/phone/bridge.py :: PhoneAIBridge._execute_tool)
# used to hard-``return f"Unknown tool: {name}"`` for any name not in its
# ``unified_tool_map`` -- and the six per-provider web tools are NOT in that map,
# so phone web search silently broke. The fix removes the dead ``web_search`` map
# entry and converts the fallthrough into a catch-all that routes any unmapped
# ToolVault tool through the unified executor by its own name.


def test_phone_bridge_source_has_no_web_search_map_and_no_hard_unknown():
    """Source-level guard: the dead ``web_search`` map entry is gone and the
    bridge no longer hard-returns "Unknown tool" without a catch-all."""
    text = (REPO_ROOT / "Orchestrator/phone/bridge.py").read_text(encoding="utf-8")
    assert '"web_search": "web_search"' not in text, (
        "phone bridge still maps the deleted generic web_search tool."
    )
    assert 'return f"Unknown tool: {name}"' not in text, (
        "phone bridge still hard-returns 'Unknown tool' instead of a catch-all."
    )
    assert "unified_name = name" in text, (
        "phone bridge is missing the catch-all that routes unmapped ToolVault "
        "tools by their own name."
    )


def test_phone_bridge_routes_unmapped_web_tool_through_executor(monkeypatch):
    """``PhoneAIBridge._execute_tool`` must pass an UNMAPPED web tool name
    (e.g. ``gemini_web_search``) straight through to the unified executor by its
    own name -- this is what makes phone web search work again."""
    import Orchestrator.tools.blackbox_tools as bbt
    from Orchestrator.phone.bridge import PhoneAIBridge

    captured = {}

    class _FakeResult:
        def rich_result(self):
            return "EXECUTOR_RAN"

    async def _fake_execute_tool(tool_name, tool_input, operator):
        captured["tool_name"] = tool_name
        captured["operator"] = operator
        captured["input"] = tool_input
        return _FakeResult()

    # _execute_tool imports `execute_tool` from this module at call time.
    monkeypatch.setattr(bbt, "execute_tool", _fake_execute_tool)

    # Build a bridge without running the heavy __init__; _execute_tool only
    # touches self.phone_session.operator.
    bridge = object.__new__(PhoneAIBridge)
    bridge.phone_session = type("S", (), {"operator": "Brandon"})()

    out = asyncio.run(
        bridge._execute_tool("gemini_web_search", {"query": "tracked robot"})
    )

    assert out == "EXECUTOR_RAN"
    # Unmapped name routed through by its OWN name (catch-all), not dropped.
    assert captured["tool_name"] == "gemini_web_search"
    assert captured["operator"] == "Brandon"
    assert captured["input"] == {"query": "tracked robot"}
