"""The chat streams run the media tools off the event loop.

Audit O02-chat-routes-A-12: list_media and search_media run
media_index.sync_uploads_folder() - an iterdir over Portal/uploads (thousands
of files on a used box) plus the index load - and get_media looks the task up
in the task DB and stats the file. All five chat streams (OpenAI, Anthropic,
Gemini, xAI, custom) called them inline from their async generators, so a model
listing media froze every other stream, voice call and keepalive on the one
uvicorn loop for the length of the scan.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

SYNC_MEDIA_TOOLS = {"execute_get_media", "execute_list_media", "execute_search_media"}
STREAMS = ("stream_openai_with_reasoning", "stream_anthropic_with_thinking",
           "stream_gemini_with_thinking", "stream_xai_with_reasoning",
           "stream_custom_with_reasoning")


def _chat_routes_tree():
    src = Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    return ast.parse(src.read_text(encoding="utf-8"))


def _direct_calls(fn: ast.AST) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in SYNC_MEDIA_TOOLS):
            out.append((node.func.id, node.lineno))
    return out


def test_no_async_function_calls_a_media_tool_inline():
    tree = _chat_routes_tree()
    inline = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            inline += [(fn.name, name, line) for name, line in _direct_calls(fn)]
    assert inline == [], f"media tools called on the event loop: {inline}"


@pytest.mark.parametrize("stream", STREAMS)
def test_every_stream_still_offers_the_three_media_tools(stream):
    """The guard above is not vacuous: each stream hands the three tools to
    run_blocking."""
    tree = _chat_routes_tree()
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == stream)
    handed = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "run_blocking" and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id in SYNC_MEDIA_TOOLS):
            handed.add(node.args[0].id)
    assert handed == SYNC_MEDIA_TOOLS


def test_run_blocking_runs_the_scan_on_a_worker_thread(monkeypatch):
    """What the streams now await: the scan runs on a pool thread, not the loop's."""
    from Orchestrator.routes import chat_routes as cr
    seen = {}

    def fake_list(media_type=None, limit=20):
        seen["thread"] = threading.current_thread()
        return {"count": 0, "media": []}

    async def main():
        seen["loop_thread"] = threading.current_thread()
        return await cr.run_blocking(fake_list, media_type=None, limit=5)

    assert asyncio.run(main()) == {"count": 0, "media": []}
    assert seen["thread"] is not seen["loop_thread"]
