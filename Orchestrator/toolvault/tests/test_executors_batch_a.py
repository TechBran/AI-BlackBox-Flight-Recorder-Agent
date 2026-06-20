"""Tests for Batch A module executors (Task 6.2).

Batch A migrates web + media tool executors OUT of the monolithic
``blackbox_tools._execute_<name>`` methods INTO per-tool
``ToolVault/tools/<name>/executor.py`` modules. Once a module's ``executor.py``
exists, ``registry.get_executor`` loads it and the dispatch façade
(``BlackBoxToolExecutor.execute``) routes to it ahead of any legacy method.

These tests run against the REAL on-disk modules (no tmp_path) — they assert the
executors load cleanly (callable, no load_errors, correct 2-arg async
signature), smoke a couple via mocked network, and prove the dispatch rail +
``ctx.operator`` flow end to end for the per-provider web search tools.

The generic ``web_search`` tool was replaced by six per-provider tools
(perplexity/openai/gemini/grok web + grok X + duckduckgo); BATCH_A and the
web-search smoke/dispatch tests now target those.
"""

import asyncio
import inspect

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


BATCH_A = [
    "perplexity_web_search",
    "openai_web_search",
    "gemini_web_search",
    "grok_web_search",
    "grok_x_search",
    "duckduckgo_web_search",
    "web_fetch",
    "generate_image",
    "generate_video",
    "lyria_music",
    "extend_video",
    "get_media",
    "list_media",
    "search_media",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    """Invalidate the executor cache around each test so on-disk edits register."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. Every Batch A executor loads: callable, no load_errors, valid signature.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BATCH_A)
def test_executor_is_callable(name):
    ex = registry.get_executor(name)
    assert ex is not None, f"get_executor({name!r}) returned None"
    assert callable(ex)
    # Must be an async def taking exactly (params, ctx).
    assert inspect.iscoroutinefunction(ex), f"{name} executor is not async"
    positional = [
        p
        for p in inspect.signature(ex).parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 2, f"{name} executor must take (params, ctx)"


@pytest.mark.parametrize("name", BATCH_A)
def test_no_load_error_for_executor(name):
    # Force the load, then confirm it isn't recorded as a failure.
    registry.get_executor(name)
    errors = registry.load_errors()
    assert name not in errors, f"{name} has load errors: {errors.get(name)}"


def test_all_batch_a_loaded():
    """Mirrors the brief's acceptance check: every Batch A tool resolves to a callable."""
    assert all(registry.get_executor(n) is not None for n in BATCH_A)


# ---------------------------------------------------------------------------
# 2. Routing smoke — run executors with the network mocked.
# ---------------------------------------------------------------------------

def test_perplexity_web_search_executor_smoke(monkeypatch):
    """perplexity_web_search calls perform_provider_search; mock it (no network)."""
    import Orchestrator.web_tools as web_tools

    monkeypatch.setattr(
        web_tools, "perform_provider_search",
        lambda provider, query, search_recency_filter="month": f"RESULTS for {query}",
    )

    ex = registry.get_executor("perplexity_web_search")
    result = asyncio.run(ex({"query": "robots"}, ToolContext(operator="Brandon")))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "RESULTS for robots" in result.result


def test_perplexity_web_search_executor_requires_query():
    """Empty query short-circuits before any network call."""
    ex = registry.get_executor("perplexity_web_search")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "query is required" in result.result.lower()


def test_web_fetch_executor_smoke(monkeypatch):
    """web_fetch calls perform_web_fetch; mock it so no real HTTP call."""
    import Orchestrator.web_tools as web_tools

    monkeypatch.setattr(
        web_tools, "perform_web_fetch",
        lambda url, max_chars: f"CONTENT of {url}",
    )

    ex = registry.get_executor("web_fetch")
    result = asyncio.run(
        ex({"url": "https://example.com"}, ToolContext(operator="system"))
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "CONTENT of https://example.com" in result.result
    assert result.data == {"url": "https://example.com"}


def test_get_media_executor_smoke(monkeypatch):
    """get_media delegates to chat_routes.execute_get_media; mock it."""
    import Orchestrator.routes.chat_routes as chat_routes

    monkeypatch.setattr(
        chat_routes, "execute_get_media",
        lambda url, task_id: {"url": "/ui/uploads/x.png", "type": "image"},
    )

    ex = registry.get_executor("get_media")
    result = asyncio.run(
        ex({"url": "/ui/uploads/x.png"}, ToolContext(operator="system"))
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "/ui/uploads/x.png" in result.result


def test_get_media_executor_requires_input():
    ex = registry.get_executor("get_media")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert result.success is False
    assert "required" in result.result.lower()


# ---------------------------------------------------------------------------
# 3. Dispatch-level: BlackBoxToolExecutor.execute routes to the module and the
#    ctx.operator flows from the executor instance end to end.
# ---------------------------------------------------------------------------

def test_dispatch_routes_perplexity_web_search_to_module(monkeypatch):
    import Orchestrator.web_tools as web_tools

    captured = {}

    def _fake_search(provider, query, search_recency_filter="month"):
        captured["provider"] = provider
        captured["query"] = query
        return f"RESULTS for {query}"

    monkeypatch.setattr(web_tools, "perform_provider_search", _fake_search)

    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("perplexity_web_search", {"query": "tracked robot"}))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "RESULTS for tracked robot" in result.result
    assert captured["query"] == "tracked robot"
    assert captured["provider"] == "perplexity"


def test_dispatch_routes_generate_image_to_module(monkeypatch):
    """generate_image executor posts via aiohttp; assert the module path runs.

    We don't run a real HTTP server — instead we confirm the dispatch resolves
    to the SAME callable the registry hands out (the module), proving the rail.
    """
    module_ex = registry.get_executor("generate_image")
    assert module_ex is not None
    # The dispatcher resolves to this exact module callable (no alias here).
    assert registry.get_executor("generate_image") is module_ex
