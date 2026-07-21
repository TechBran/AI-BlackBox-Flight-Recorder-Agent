import asyncio
import time

from Orchestrator import local_stack


def test_gate_open_when_no_voice_session():
    async def scenario():
        assert local_stack.is_voice_active() is False
        async with local_stack.retrieval_gate(timeout=1.0):
            return "ran"
    assert asyncio.run(scenario()) == "ran"


def test_voice_session_blocks_then_releases_gate():
    async def scenario():
        order = []

        async def retriever():
            async with local_stack.retrieval_gate(timeout=5.0):
                order.append("retrieval")

        async def voice():
            async with local_stack.voice_session():
                assert local_stack.is_voice_active() is True
                order.append("voice-start")
                await asyncio.sleep(0.2)   # gate must wait through this
                order.append("voice-end")
        await asyncio.gather(voice(), retriever())
        return order
    order = asyncio.run(scenario())
    # retrieval ran only AFTER the voice session closed.
    assert order == ["voice-start", "voice-end", "retrieval"], order
    assert local_stack.is_voice_active() is False


def test_bounded_gate_times_out_under_a_held_session():
    async def scenario():
        async with local_stack.voice_session():
            t0 = time.monotonic()
            try:
                async with local_stack.retrieval_gate(timeout=0.1):
                    return "ran"      # must NOT happen
            except asyncio.TimeoutError:
                return round(time.monotonic() - t0, 2)
    elapsed = asyncio.run(scenario())
    assert isinstance(elapsed, float) and elapsed >= 0.1


def test_reentrant_depth_counter():
    async def scenario():
        async with local_stack.voice_session():
            async with local_stack.voice_session():
                assert local_stack.is_voice_active() is True
            assert local_stack.is_voice_active() is True   # still one open
        assert local_stack.is_voice_active() is False
    asyncio.run(scenario())
