"""Phone call legs run on the shared live-voice bridge (audit rank 74,
X05-architecture-13; O10-telephony-twilio-09).

The phone bridge cloned each provider's session handling again, and the copies
had drifted from the routes:

  * Gemini: the leg ran the route's handler through a PhoneWebSocketAdapter
    that had no ``application_state``, so the route's send helper dropped every
    frame: the caller never heard the model.
  * OpenAI: the leg listened for the Beta event names (response.audio.delta)
    the GA API renamed, so the caller never heard the model either.
  * Gemini audio went up as the deprecated realtimeInput.mediaChunks, and the
    Grok ULAW path sent 24 kHz audio to a session that declares 16 kHz input.
  * A hangup never closed the provider socket; a send on a dead socket spawned
    a reconnect task per 20 ms frame; a Grok reconnect dropped the phone VAD.

Every live leg now goes through the provider's LiveVoiceBridge adapter: the
same listener, tool ladder and transcript as the Portal, with this bridge
owning the reconnect loop and the audio conversion.
"""
import asyncio
import base64
import json

import pytest

from Orchestrator import models
from Orchestrator.phone.audio_converter import AudioConverter
from Orchestrator.tests import voice_golden_harness as harness
from Orchestrator.toolvault.context import ToolResult

PROVIDERS = ["gemini", "openai", "grok"]
WS_ATTR = {"gemini": "gemini_ws", "openai": "openai_ws", "grok": "grok_ws"}
REGISTRY = {"gemini": "GEMINI_LIVE_SESSIONS", "openai": "REALTIME_SESSIONS",
            "grok": "GROK_LIVE_SESSIONS"}
MODEL_AUDIO = b"\x05\x00" * 240          # 10 ms of 24 kHz PCM16 from the model
MODEL_AUDIO_B64 = base64.b64encode(MODEL_AUDIO).decode()


def _backend(provider):
    from Orchestrator.phone.session import AIBackend
    return {"gemini": AIBackend.GEMINI_LIVE, "openai": AIBackend.OPENAI_REALTIME,
            "grok": AIBackend.GROK_LIVE}[provider]


def _reply(provider, text="Hello caller."):
    """The model's spoken reply, in the provider's GA vocabulary."""
    if provider == "gemini":
        return [
            {"serverContent": {"outputTranscription": {"text": text}}},
            {"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": MODEL_AUDIO_B64}}]}}},
            {"serverContent": {"turnComplete": True}},
        ]
    return [
        {"type": "response.output_audio.delta", "delta": MODEL_AUDIO_B64},
        {"type": "response.output_audio_transcript.delta", "delta": text},
        {"type": "response.output_audio.done"},
        {"type": "response.output_audio_transcript.done", "transcript": text},
        {"type": "response.done", "response": {"status": "completed"}},
    ]


def _first_audio_matcher(provider):
    return harness.On("realtime_input") if provider == "gemini" else \
        harness.On("input_audio_buffer.append")


class _Call:
    """A PhoneAIBridge call leg wired to the harness's scripted upstreams."""

    def __init__(self, provider, upstreams):
        from Orchestrator.phone.bridge import PhoneAIBridge
        from Orchestrator.phone.session import PhoneSession
        self.provider = provider
        self.patches, self.cap = harness.Patches(), harness.Capture()
        harness.install(self.patches, self.cap)
        harness.FakeExecutor.answers = {"roll_dice": ToolResult(success=True, result="rolled 4")}
        self.cap.pending = list(upstreams)
        self.bridge = PhoneAIBridge(PhoneSession(session_id=f"leg-{provider}",
                                                 ai_backend=_backend(provider)))
        self.played = []
        self.transcripts = []

        async def on_pcm16(pcm, rate):
            self.played.append((pcm, rate))

        async def on_transcript(text):
            self.transcripts.append(text)

        self.bridge.on_ai_pcm16 = on_pcm16
        self.bridge.on_ai_transcript = on_transcript

    def model_audio_played(self):
        return [p for p in self.played if p == (MODEL_AUDIO, 24000)]

    def close(self):
        self.patches.undo()


def _run(provider, upstreams, body):
    async def run():
        call = _Call(provider, upstreams)
        try:
            assert await call.bridge.start(operator=harness.OPERATOR)
            await harness.settle(call.cap)
            await body(call)
            return call
        finally:
            if call.bridge.is_running:
                await call.bridge.stop()
            call.close()
    return asyncio.run(run())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_caller_hears_the_model(provider):
    up = harness.ScriptedUpstream([(_first_audio_matcher(provider), _reply(provider))])

    async def body(call):
        await call.bridge.send_pcm16(b"\x03\x00" * 160, 16000)
        await harness.settle(call.cap)

    call = _run(provider, [up], body)
    assert call.model_audio_played(), f"{provider}: the model's audio never reached the call"
    assert call.transcripts == ["Hello caller."]
    assert call.cap.saves, "the call's transcript is saved at hangup"
    assert "Hello caller." in call.cap.saves[0]["session_summary"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_caller_audio_goes_up_in_the_provider_frame_at_its_input_rate(provider):
    up = harness.ScriptedUpstream()

    async def body(call):
        ulaw = AudioConverter.generate_silence(duration_ms=20, sample_rate=8000)
        await call.bridge.send_audio(ulaw)
        await harness.settle(call.cap)

    _run(provider, [up], body)
    audio = [f for f in up.sent if "realtime_input" in f or f.get("type") == "input_audio_buffer.append"]
    assert len(audio) == 1, up.sent
    rate = {"gemini": 16000, "openai": 24000, "grok": 16000}[provider]
    if provider == "gemini":
        chunk = audio[0]["realtime_input"]["audio"]
        assert chunk["mimeType"] == f"audio/pcm;rate={rate}"
        data = chunk["data"]
    else:
        data = audio[0]["audio"]
    # 20 ms of PCM16 at the rate the session was configured with.
    assert len(base64.b64decode(data)) == rate // 50 * 2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_tool_call_on_the_line_is_answered(provider):
    if provider == "gemini":
        call_frames = [{"toolCall": {"functionCalls": [
            {"id": "pf-1", "name": "roll_dice", "args": {"sides": 6}}]}}]
    else:
        call_frames = [{"type": "response.function_call_arguments.done", "call_id": "pc-1",
                        "name": "roll_dice", "arguments": "{\"sides\": 6}"}]
    up = harness.ScriptedUpstream([(_first_audio_matcher(provider), call_frames)])

    async def body(call):
        await call.bridge.send_pcm16(b"\x03\x00" * 160, 16000)
        await harness.settle(call.cap)

    call = _run(provider, [up], body)
    answers = [f for f in up.sent if "toolResponse" in f
               or (f.get("type") == "conversation.item.create"
                   and f["item"].get("type") == "function_call_output")]
    assert len(answers) == 1, up.sent
    text = json.dumps(answers[0])
    assert "rolled 4" in text
    assert call.cap.result()["executor_calls"] == [
        {"tool": "roll_dice", "args": {"sides": 6}, "operator": harness.OPERATOR}]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_hanging_up_closes_the_provider_socket(provider):
    up = harness.ScriptedUpstream()

    async def body(call):
        await call.bridge.stop()

    call = _run(provider, [up], body)
    assert up.closed, f"{provider}: the provider socket outlived the call"
    assert getattr(call.bridge._ai_session, WS_ATTR[provider]) is None
    assert f"phone-leg-{provider}" not in getattr(models, REGISTRY[provider])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_dropped_leg_is_redialed_once_with_the_phone_tuning(provider):
    first = harness.ScriptedUpstream([(_first_audio_matcher(provider), [
        harness.Close(1011, "internal error")])])
    second = harness.ScriptedUpstream()

    async def body(call):
        await call.bridge.send_pcm16(b"\x03\x00" * 160, 16000)
        await harness.settle(call.cap)

    call = _run(provider, [first, second], body)
    assert len(call.cap.dials) == 2, "exactly one dialer: the phone bridge's reconnect loop"
    configure = second.sent[0]
    if provider == "gemini":
        assert configure["setup"]["realtimeInputConfig"]["automaticActivityDetection"][
            "silenceDurationMs"] == 1000, "phone_mode VAD lost on reconnect"
    elif provider == "grok":
        vad = [f for f in second.sent if f.get("type") == "session.update"
               and f["session"].get("turn_detection", {}).get("threshold") == 0.8]
        assert vad, "the phone VAD tuning is re-applied on the redialed leg"
    else:
        assert configure["session"]["audio"]["input"]["noise_reduction"] == {"type": "near_field"}


@pytest.mark.parametrize("provider", ["openai", "grok"])
def test_sends_on_a_dead_socket_do_not_spawn_reconnects(provider):
    """Each 20 ms frame on a dead socket spawned another reconnect task (~50/s)
    until the caller hung up (O10-telephony-twilio-09)."""
    import websockets
    from websockets.frames import Close as WsClose

    class _Dead(harness.ScriptedUpstream):
        async def send(self, payload):
            frame = json.loads(payload)
            if frame.get("type") == "input_audio_buffer.append":
                raise websockets.exceptions.ConnectionClosedError(WsClose(1011, "gone"), None)
            await super().send(payload)

    up = _Dead()

    async def body(call):
        ulaw = AudioConverter.generate_silence(duration_ms=20, sample_rate=8000)
        for _ in range(25):
            await call.bridge.send_audio(ulaw)
            await asyncio.sleep(0)
        await harness.settle(call.cap)

    call = _run(provider, [up], body)
    assert len(call.cap.dials) == 1, "a failed send must leave the reconnect to the listener"
