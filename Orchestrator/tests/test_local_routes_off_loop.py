"""Audit rank 46 (O17-toolvault-runtime-02, with its sibling O19-local-stack-01):
the on-device provider's routes never stall the server's event loop.

Every on-device Gemma turn calls /local/turn/prepare, which builds a fossil
context (embed + retrieval + rerank) and injects tools (a query embed);
/local/tools/search embeds too, and /local/models/catalog makes 1+N blocking
Hugging Face requests (20 s timeout each) on a cache miss. All of it ran inline
in async handlers; now it runs in worker threads.

The tests measure the longest gap between turns of a 20 ms ticker: a call
blocking the loop for 0.4 s shows up as a 0.4 s gap. Hermetic: every slow
call is a stub.
"""
import asyncio
import time

BLOCK = 0.4


async def _max_gap_during(coro):
    """Await ``coro`` while a 20 ms ticker runs; return (result, longest gap)."""
    gaps = []
    stop = asyncio.Event()
    ticking = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            ticking.set()

    t = asyncio.create_task(ticker())
    await ticking.wait()  # a call that blocks at once must land between ticks
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, max(gaps or [0.0])


def _slow(ret):
    def stub(*a, **k):
        time.sleep(BLOCK)
        return ret
    return stub


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


TOOL = {"name": "roll_dice", "description": "Roll dice", "parameters": {}}


def test_turn_prepare_builds_context_and_tools_off_the_loop(monkeypatch):
    import Orchestrator.routes.local_routes as lr
    monkeypatch.setattr(lr, "build_fossil_context", _slow(("FOSSILS", {"semantic": ["s1"]})))
    monkeypatch.setattr(lr, "build_injected_tools", _slow([TOOL]))
    result, gap = asyncio.run(_max_gap_during(
        lr.local_turn_prepare(_Req({"operator": "local-op", "prompt": "hi"}))))
    assert result["success"] is True and result["tools"] == [TOOL]
    assert result["system_prompt"].endswith("FOSSILS")
    assert result["provenance"]["semantic"] == ["s1"]
    assert gap < 0.2, f"/local/turn/prepare blocked the event loop for {gap:.2f}s"


def test_tools_search_runs_off_the_loop(monkeypatch):
    import Orchestrator.routes.local_routes as lr
    monkeypatch.setattr(lr, "build_injected_tools", _slow([TOOL]))
    result, gap = asyncio.run(_max_gap_during(
        lr.local_tools_search(_Req({"query": "roll a die"}))))
    assert result == {"success": True, "tools": [TOOL]}
    assert gap < 0.2, f"/local/tools/search blocked the event loop for {gap:.2f}s"


def test_tools_search_query_embed_runs_off_the_loop(monkeypatch):
    """Through the real discovery path: meta_tool search -> embed_query."""
    import Orchestrator.routes.local_routes as lr
    import Orchestrator.toolvault.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", _slow(None))
    result, gap = asyncio.run(_max_gap_during(
        lr.local_tools_search(_Req({"query": "roll a die"}))))
    assert result["success"] is True
    assert gap < 0.2, f"/local/tools/search's embed blocked the event loop for {gap:.2f}s"


def test_models_catalog_fetch_runs_off_the_loop(monkeypatch):
    import Orchestrator.routes.local_routes as lr
    bundles = [{"slug": "gemma-x"}]
    monkeypatch.setattr(lr.catalog, "list_bundles", _slow(bundles))
    result, gap = asyncio.run(_max_gap_during(lr.local_models_catalog()))
    assert result == {"bundles": bundles}
    assert gap < 0.2, f"/local/models/catalog blocked the event loop for {gap:.2f}s"
