"""Model-supplied numbers are read once, where they are used (audit rank 74,
O03-chat-routes-B-07).

Models stringify numbers. The shared dispatcher coerced the native branches'
arguments, but the helpers themselves did not: execute_list_media's
min(limit or 20, 50) and execute_search_media's min(limit or 10, 20) raised
TypeError on limit="5" for every caller that hands the raw value over (the
Computer Use driver did, and the three live-voice routes and the ToolVault
media executors still do), and perform_web_fetch's len(text) > max_chars did
the same on max_chars="5000": the fetch failed and the model was told not to
retry a page it could have read. Each helper now reads the number through
utils.tool_args.int_value, so every caller is safe.

Hermetic: the media index and the network fetch are stubbed.
"""
import asyncio
import types

import pytest

from Orchestrator import media_index, web_tools
from Orchestrator.routes import chat_routes as cr
from Orchestrator.utils.tool_args import int_value


@pytest.mark.parametrize("raw, expected", [
    (5, 5), (5.0, 5), ("5", 5), (" 5 ", 5), ("5.0", 5), ("-2", -2),
    (None, 7), ("", 7), ("five", 7), (2.5, 7), ("2.5", 7), (True, 7), ([5], 7),
    ("inf", 7), ("nan", 7),
])
def test_int_value_reads_what_models_send(raw, expected):
    assert int_value(raw, 7) == expected


def test_int_value_clamps_after_reading():
    assert int_value("90", 7, lo=1, hi=50) == 50
    assert int_value("0", 7, lo=1, hi=50) == 1
    assert int_value("junk", 70, hi=50) == 50


@pytest.fixture
def index(monkeypatch):
    calls = []
    monkeypatch.setattr(media_index, "sync_uploads_folder", lambda: None)
    monkeypatch.setattr(media_index, "get_index_stats", lambda: {"total_files": 0})
    monkeypatch.setattr(media_index, "list_media",
                        lambda media_type=None, limit=20: calls.append(("list", limit)) or [])
    monkeypatch.setattr(media_index, "search_media",
                        lambda query, media_type=None, limit=10: calls.append(("search", limit)) or [])
    return calls


@pytest.mark.parametrize("limit, expected", [("5", 5), (5, 5), (" 5 ", 5), ("junk", 20),
                                             (None, 20), (0, 20), ("0", 20), (500, 50), ("500", 50)])
def test_list_media_reads_a_stringified_limit(index, limit, expected):
    assert cr.execute_list_media(limit=limit)["count"] == 0
    assert index == [("list", expected)]


@pytest.mark.parametrize("limit, expected", [("5", 5), (5, 5), ("junk", 10), (None, 10),
                                             (0, 10), (99, 20), ("99", 20)])
def test_search_media_reads_a_stringified_limit(index, limit, expected):
    assert cr.execute_search_media(query="fox", limit=limit)["count"] == 0
    assert index == [("search", expected)]


def test_the_toolvault_media_executors_survive_a_stringified_limit(index):
    """The ToolVault list_media / search_media executors pass the model's limit
    straight through (the catch-all and MCP surfaces)."""
    from Orchestrator.toolvault import registry
    from Orchestrator.toolvault.context import ToolContext

    for name, params in (("list_media", {"limit": "5"}), ("search_media", {"query": "fox", "limit": "5"})):
        execute = registry.get_executor(name)
        result = asyncio.run(execute(params, ToolContext(operator="system")))
        assert result.success, result.result
    assert index == [("list", 5), ("search", 5)]


@pytest.fixture
def page(monkeypatch):
    html = "<html><head><title>T</title></head><body><main>" + ("word " * 2000) + "</main></body></html>"
    monkeypatch.setattr(web_tools.safe_fetch, "safe_get",
                        lambda url, **k: types.SimpleNamespace(status_code=200, content=html.encode()))
    monkeypatch.setattr(web_tools, "_check_rate_limit", lambda: True)
    monkeypatch.setattr(web_tools, "_get_cache", lambda key: None)
    monkeypatch.setattr(web_tools, "_set_cache", lambda key, value, ttl: None)


@pytest.mark.parametrize("max_chars", ["5000", " 5000 ", 5000.0])
def test_web_fetch_reads_a_stringified_max_chars(page, max_chars):
    result = web_tools.perform_web_fetch("https://example.com/", max_chars)
    assert "Successfully fetched" in result
    assert "showing 5000 of 9999 total characters" in result


@pytest.mark.parametrize("max_chars", ["lots", None, [1]])
def test_web_fetch_falls_back_to_its_default_for_a_non_number(page, max_chars):
    result = web_tools.perform_web_fetch("https://example.com/", max_chars)
    assert "Successfully fetched" in result
    assert "content truncated" not in result          # 9,999 characters < the 80,000 default
