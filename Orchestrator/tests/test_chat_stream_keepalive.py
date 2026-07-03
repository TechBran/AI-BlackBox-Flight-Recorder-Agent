"""M7 / WI-10 transport hardening: SSE keepalive during provider prefill.

`chat_routes._stream_with_keepalive` wraps every provider stream on
/chat/stream (GET + POST) and yields a ``None`` sentinel for every
KEEPALIVE_INTERVAL_S of provider silence; generate_sse renders each sentinel
as an SSE comment frame (": keepalive") — inert for compliant SSE parsers
(Android M7.1a comment-frame tolerance is test-pinned; the Portal parser
skips blocks with no event:/data: lines).

All tests hermetic: fake async generators, sub-second intervals.
"""
import asyncio

import pytest

from Orchestrator.routes import chat_routes as cr


async def _collect(stream, interval_s):
    out = []
    async for item in cr._stream_with_keepalive(stream, interval_s=interval_s):
        out.append(item)
    return out


def test_silent_prefill_emits_keepalive_sentinels():
    """A provider that stays silent past the interval produces None sentinels
    BEFORE its first event — the M3 audit's silent-TTFB window is filled."""
    async def slow_first_token():
        await asyncio.sleep(0.35)
        yield {"type": "content", "data": "OK"}

    out = asyncio.run(_collect(slow_first_token(), interval_s=0.1))
    first_event_idx = out.index({"type": "content", "data": "OK"})
    keepalives_before = out[:first_event_idx].count(None)
    assert keepalives_before >= 2, f"expected >=2 keepalives before first token, got {out}"
    assert out[-1] == {"type": "content", "data": "OK"}


def test_flowing_tokens_emit_no_keepalives():
    """While tokens flow faster than the interval, no sentinel is injected —
    the relayed stream is byte-identical to the provider stream."""
    events = [{"type": "content", "data": str(i)} for i in range(5)]

    async def fast():
        for e in events:
            yield e

    out = asyncio.run(_collect(fast(), interval_s=0.2))
    assert out == events


def test_tool_loop_followup_silence_also_covered():
    """Gap-based (not first-token-only): a mid-stream silence — the tool-loop
    follow-up prefill after a tool_result — also gets keepalives."""
    async def stream_with_midgap():
        yield {"type": "tool_result", "data": "..."}
        await asyncio.sleep(0.35)
        yield {"type": "content", "data": "answer"}

    out = asyncio.run(_collect(stream_with_midgap(), interval_s=0.1))
    i_tool = out.index({"type": "tool_result", "data": "..."})
    i_content = out.index({"type": "content", "data": "answer"})
    assert out[i_tool + 1:i_content].count(None) >= 2


def test_provider_exception_propagates_unchanged():
    async def boom():
        yield {"type": "content", "data": "x"}
        raise RuntimeError("provider fell over")

    with pytest.raises(RuntimeError, match="provider fell over"):
        asyncio.run(_collect(boom(), interval_s=0.5))


def test_empty_stream_terminates_cleanly():
    async def empty():
        if False:  # pragma: no cover — makes this an async generator
            yield None

    assert asyncio.run(_collect(empty(), interval_s=0.05)) == []


def test_early_consumer_close_cancels_pending_provider_task():
    """If the SSE consumer disconnects while the provider is still silent, the
    pending anext task must be cancelled (no leaked task / 'never retrieved'
    warnings)."""
    cancelled = asyncio.Event()

    async def hang_forever():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield {"type": "content", "data": "never"}  # pragma: no cover

    async def run():
        gen = cr._stream_with_keepalive(hang_forever(), interval_s=0.05)
        first = await gen.__anext__()
        assert first is None  # got a keepalive while the provider hangs
        await gen.aclose()    # consumer disconnects
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    asyncio.run(run())


def test_keepalive_frame_is_an_sse_comment():
    """The rendered frame must be a comment frame (leading colon), NOT an
    event — the Portal binds a CU-specific 'heartbeat' event, so a named event
    could misfire client handlers."""
    assert cr.SSE_KEEPALIVE_FRAME.startswith(":")
    assert cr.SSE_KEEPALIVE_FRAME.endswith("\n\n")
    assert "event:" not in cr.SSE_KEEPALIVE_FRAME
