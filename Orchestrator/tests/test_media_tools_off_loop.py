"""Audit rank 46 (O18-toolvault-modules-03): the media tools never run on the
server's event loop.

get_media, list_media and search_media are dispatched by name in all five chat
stream loops and the three live-voice bridges, ahead of the ToolVault executor
catch-all, so threading the executors did not cover them. list_media and
search_media rescan the uploads folder (3,000+ files) and re-read the media
index, rewriting it when files are unindexed; get_media reads the index too.
These calls go through run_blocking now.

A static check over every async function in Orchestrator/, plus a runtime check
that a stubbed slow helper leaves a 20 ms ticker running.
"""
import ast
import asyncio
import pathlib
import time

ORCH = pathlib.Path(__file__).resolve().parents[1]
MEDIA_HELPERS = {"execute_get_media", "execute_list_media", "execute_search_media"}
# A file whose async code runs on a loop of its own in a worker thread.
OWN_LOOP = {"browser/headless.py"}


def _inline_media_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []

    def walk(node, in_async):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)
            else:
                if in_async and isinstance(child, ast.Call):
                    f = child.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if name in MEDIA_HELPERS:
                        hits.append(f"{path.relative_to(ORCH)}:{child.lineno}: {ast.unparse(child)[:80]}")
                walk(child, in_async)

    walk(tree, False)
    return hits


def test_no_async_code_runs_a_media_tool_inline():
    hits = []
    for path in sorted(ORCH.rglob("*.py")):
        rel = path.relative_to(ORCH).as_posix()
        if rel.startswith(("venv/", "tests/")) or "/tests/" in rel or rel in OWN_LOOP:
            continue
        hits += _inline_media_calls(path)
    assert not hits, "media tools called inline in async code:\n" + "\n".join(hits)


def test_a_slow_media_scan_leaves_the_loop_running(monkeypatch):
    """run_blocking, as the dispatch sites now use it, keeps the loop ticking
    through a slow uploads scan."""
    from Orchestrator.routes import chat_routes
    from Orchestrator.utils.async_helpers import run_blocking

    def slow_scan(media_type=None, limit=20):
        time.sleep(0.4)
        return {"count": 0, "media": []}

    monkeypatch.setattr(chat_routes, "execute_list_media", slow_scan)

    async def go():
        gaps, stop = [], asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        result = await run_blocking(chat_routes.execute_list_media, media_type=None, limit=5)
        stop.set()
        await t
        return result, max(gaps)

    result, gap = asyncio.run(go())
    assert result == {"count": 0, "media": []}
    assert gap < 0.2, f"the media scan blocked the event loop for {gap:.2f}s"
