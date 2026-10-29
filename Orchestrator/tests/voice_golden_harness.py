"""Record-and-replay harness for the live-voice bridges' golden fixtures.

The three /ws/<provider> bridges (Gemini Live, OpenAI Realtime, xAI Grok) and
the phone bridge are driven end to end against a SCRIPTED upstream: a fake
provider socket that answers what the bridge sends it with frames shaped like a
recorded provider session. Everything the bridge says is captured:

  * the client-facing WebSocket frames, in order (what the Portal and the
    Android app receive),
  * every upstream dial (URL and header names) and the frames sent on each,
  * the transcript saves, the tool executor calls and the tasks created.

That capture is compared with a JSON golden under golden/voice/. The goldens
were recorded on the code before the bridges were consolidated behind one
state machine (audit rank 74, X05-architecture-13); a refactor must keep them
byte-identical. Re-record (only for a deliberate wire change) with

    python -m Orchestrator.tests.voice_golden_harness [scenario ...]

from the repo root. Hermetic: no network (the dial is faked), no stores (the
retrieval, the persona, the tools catalog, the contacts, the task DB and the
transcript save are stubbed) and no wall clock in the captured frames.

Stubs are installed on EVERY voice module that binds the patched name, so the
harness keeps working when code moves between the route modules and the
shared voice package.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import datetime as _datetime_mod
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "voice"

# Every module that may bind a name the harness stubs. Missing ones are
# skipped, so the list can name modules that only exist after a refactor.
VOICE_MODULES = (
    "Orchestrator.routes.gemini_live_routes",
    "Orchestrator.routes.grok_live_routes",
    "Orchestrator.routes.realtime_routes",
    "Orchestrator.routes.voice_ws_shared",
    "Orchestrator.routes.voice_prompts",
    "Orchestrator.voice",
    "Orchestrator.voice.bridge",
    "Orchestrator.voice.tools",
    "Orchestrator.voice.context",
    "Orchestrator.phone.bridge",
)

FIXED_NOW = "2026-10-29T12:00:00Z"
OPERATOR = "golden-op"
CONTEXT_BLOCK = "RECENT SNAPSHOTS:\n[SNAP-20261001-0001] golden context line"
PROVENANCE = {"recent": ["SNAP-20261001-0001"], "keyword": [], "semantic": [],
              "checkpoint": ["CHK-20261001-0001"]}
_REAL_SLEEP = asyncio.sleep


# =============================================================================
# Patching (works with and without pytest)
# =============================================================================

class Patches:
    """setattr with undo, for the recorder (no pytest) and the tests alike."""

    def __init__(self):
        self._undo: List[Callable[[], None]] = []

    def setattr(self, target, name: str, value) -> None:
        missing = object()
        old = getattr(target, name, missing)
        setattr(target, name, value)
        if old is missing:
            self._undo.append(lambda: delattr(target, name))
        else:
            self._undo.append(lambda: setattr(target, name, old))

    def everywhere(self, name: str, value) -> None:
        """Patch `name` on every voice module that binds it."""
        for mod in voice_modules():
            if hasattr(mod, name):
                self.setattr(mod, name, value)

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


def voice_modules():
    mods = []
    for name in VOICE_MODULES:
        try:
            mods.append(importlib.import_module(name))
        except ImportError:
            continue
    # Per-provider modules of the shared voice package, whatever they are called.
    pkg = sys.modules.get("Orchestrator.voice")
    if pkg is not None:
        for key, mod in list(sys.modules.items()):
            if key.startswith("Orchestrator.voice.") and mod is not None and mod not in mods:
                mods.append(mod)
    return mods


# =============================================================================
# Fakes
# =============================================================================

class _Eof:
    pass


_EOF = _Eof()


class Close:
    """Script action: the provider closes the socket with this code/reason."""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason


def _closed_error(code: int, reason: str):
    import websockets
    from websockets.frames import Close as WsClose
    if code == 1000:
        return websockets.exceptions.ConnectionClosedOK(WsClose(code, reason), WsClose(code, reason))
    return websockets.exceptions.ConnectionClosedError(WsClose(code, reason), None)


class On:
    """Matcher: the `nth` frame the bridge sends upstream that contains `key`
    (a top-level key, or a "type" value when it contains a dot)."""

    def __init__(self, key: str, nth: int = 1):
        self.key = key
        self.nth = nth
        self.seen = 0

    def __call__(self, frame: dict) -> bool:
        hit = (frame.get("type") == self.key) if "." in self.key else (self.key in frame)
        if hit:
            self.seen += 1
        return hit and self.seen == self.nth


class ScriptedUpstream:
    """A provider WebSocket that answers the bridge from a script.

    ``script`` is a list of ``(matcher, replies)``: when a frame the bridge
    sends satisfies the FIRST pending step's matcher, that step's replies are
    queued for the bridge's listener to read. A reply is a JSON frame (dict)
    or a Close() that ends the socket with that code. ``on_open`` frames are
    readable right away.
    """

    def __init__(self, script=None, on_open=None):
        self.sent: List[dict] = []
        self.closed = False
        self._script = list(script or [])
        self._inbox: asyncio.Queue = asyncio.Queue()
        for frame in on_open or []:
            self._inbox.put_nowait(frame)

    async def send(self, payload):
        frame = json.loads(payload)
        self.sent.append(frame)
        for i, (matcher, replies) in enumerate(self._script):
            if matcher(frame):
                del self._script[i]
                for reply in replies:
                    self._inbox.put_nowait(reply)
                break

    async def close(self, *a, **k):
        if not self.closed:
            self.closed = True
            self._inbox.put_nowait(_EOF)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is _EOF:
            raise _closed_error(1000, "")
        if isinstance(item, Close):
            self.closed = True
            raise _closed_error(item.code, item.reason)
        return json.dumps(item)


class FakeClient:
    """The client end of a /ws/<provider> socket (Portal / Android)."""

    def __init__(self, query: Optional[dict] = None):
        from starlette.websockets import WebSocketState
        self._state = WebSocketState
        self.query_params = dict(query or {})
        self.application_state = WebSocketState.CONNECTED
        self.sent: List[dict] = []
        self.closed = None
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        from fastapi import WebSocketDisconnect
        item = await self._inbox.get()
        if item is _EOF:
            self.application_state = self._state.DISCONNECTED
            raise WebSocketDisconnect(1000)
        return item

    async def send_json(self, data):
        self.sent.append(json.loads(json.dumps(data)))

    async def close(self, code: int = 1000, reason: str = ""):
        if self.closed is None:
            self.closed = [code, reason]
        self.application_state = self._state.DISCONNECTED
        self._inbox.put_nowait(_EOF)

    def feed(self, msg: dict) -> None:
        self._inbox.put_nowait(json.dumps(msg))

    def hang_up(self) -> None:
        self._inbox.put_nowait(_EOF)


class FakeExecutor:
    """Stand-in for BlackBoxToolExecutor: records the call, answers from a table."""

    calls: List[dict] = []
    answers: Dict[str, Any] = {}

    def __init__(self, operator: str = "system", *a, **k):
        self.operator = operator

    async def execute(self, tool_name, tool_input):
        from Orchestrator.toolvault.context import ToolResult
        FakeExecutor.calls.append({"tool": tool_name, "args": copy.deepcopy(tool_input),
                                   "operator": self.operator})
        answer = FakeExecutor.answers.get(tool_name)
        if answer is None:
            return ToolResult(success=True, result=f"{tool_name} ok")
        return answer


class FixedDatetime(_datetime_mod.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 29, 14, 5, 0, tzinfo=tz)


# =============================================================================
# Capture
# =============================================================================

class Capture:
    def __init__(self):
        self.dials: List[dict] = []
        self.upstreams: List[ScriptedUpstream] = []
        self.saves: List[dict] = []
        self.tasks: List[dict] = []
        self.tool_groups: List[str] = []
        self.phone: List[list] = []
        self.pending: List[Any] = []   # ScriptedUpstream | Exception, consumed per dial

    def result(self, client: Optional[FakeClient] = None, extra: Optional[dict] = None) -> dict:
        out = {
            "client": client.sent if client else [],
            "client_closed": client.closed if client else None,
            "dials": self.dials,
            "upstream": [u.sent for u in self.upstreams],
            "saves": self.saves,
            "executor_calls": FakeExecutor.calls,
            "tasks": self.tasks,
            "tool_groups": self.tool_groups,
        }
        if self.phone:
            out["phone"] = self.phone
        if extra:
            out.update(extra)
        return normalize(out)


def _token_map() -> List[tuple]:
    from Orchestrator import config
    from Orchestrator.behavioral_core import VOICE_DELIVERY_NOTE
    from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK
    tokens = {
        "CU_CONTROL_BLOCK": CU_CONTROL_BLOCK,
        "VOICE_DELIVERY_NOTE": VOICE_DELIVERY_NOTE,
        "GEMINI_LIVE_URL_V1ALPHA": config.gemini_live_url("v1alpha"),
        "GEMINI_LIVE_URL_V1BETA": config.gemini_live_url("v1beta"),
        "OPENAI_REALTIME_URL": config.OPENAI_REALTIME_URL,
        "GROK_LIVE_URL": config.GROK_LIVE_URL,
        "GEMINI_LIVE_TRANSLATE_MODEL": config.GEMINI_LIVE_TRANSLATE_MODEL,
        "GEMINI_LIVE_MODEL": config.GEMINI_LIVE_MODEL,
        "OPENAI_REALTIME_TRANSLATE_MODEL": config.OPENAI_REALTIME_TRANSLATE_MODEL,
        "OPENAI_REALTIME_MODEL": config.OPENAI_REALTIME_MODEL,
        "GROK_LIVE_MODEL": config.GROK_LIVE_MODEL,
        "STT_OPENAI_STREAM": config.STT_OPENAI_STREAM,
    }
    # Longest first, so a value that contains another is replaced whole.
    return sorted(((v, f"<{k}>") for k, v in tokens.items() if v), key=lambda p: -len(p[0]))


def normalize(obj):
    """Config values (model ids, URLs, shared prompt blocks) become tokens, so a
    golden pins the bridge's behaviour and not this box's configuration."""
    pairs = _token_map()

    def walk(value):
        if isinstance(value, str):
            for real, token in pairs:
                if real in value:
                    value = value.replace(real, token)
            return value
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {walk(k): walk(v) for k, v in value.items()}
        return value

    return walk(json.loads(json.dumps(obj, default=str)))


# =============================================================================
# Environment
# =============================================================================

async def _fast_sleep(delay=0, result=None):
    """Backoffs and goAway delays return at once; the 10-15 s keepalive ticks
    never fire inside a scenario (they are cancelled at teardown)."""
    if delay and delay >= 10:
        await _REAL_SLEEP(3600)
    else:
        await _REAL_SLEEP(0)
    return result


def install(patches: Patches, cap: Capture) -> None:
    import websockets as real_websockets
    import websockets.exceptions  # noqa: F401 - a submodule the bridges catch from
    from Orchestrator import contacts, fossils, models, state
    from Orchestrator.routes import chat_routes  # noqa: F401 - late-imported by the tool ladders
    import Orchestrator.tools as tools_pkg

    FakeExecutor.calls = []
    FakeExecutor.answers = {}

    async def fake_dial(url, additional_headers=None, **kwargs):
        headers = dict(additional_headers or {})
        cap.dials.append({"url": url, "headers": sorted(headers)})
        if not cap.pending:
            raise OSError("no scripted upstream left for this dial")
        nxt = cap.pending.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        cap.upstreams.append(nxt)
        return nxt

    fake_ws_lib = SimpleNamespace(connect=fake_dial, exceptions=real_websockets.exceptions)
    patches.everywhere("websockets", fake_ws_lib)
    patches.everywhere("WEBSOCKETS_AVAILABLE", True)
    patches.everywhere("AIOHTTP_AVAILABLE", True)
    for key in ("GOOGLE_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
        patches.everywhere(key, "test-key")

    def fake_context(user_text, operator, log_prefix="", **kw):
        return CONTEXT_BLOCK, copy.deepcopy(PROVENANCE)

    patches.everywhere("build_fossil_context", fake_context)
    patches.everywhere("now_utc_iso", lambda: FIXED_NOW)
    patches.everywhere("get_persona", lambda operator, modality: f"PERSONA[{modality}]")

    def fake_gemini_tools(group="gemini_live"):
        cap.tool_groups.append(group)
        return [{"functionDeclarations": [{"name": f"golden_{group}_tool"}]}]

    def fake_openai_tools(group="realtime"):
        cap.tool_groups.append(group)
        return [{"type": "function", "name": f"golden_{group}_tool", "parameters": {}}]

    patches.everywhere("get_gemini_live_tools", fake_gemini_tools)
    patches.everywhere("get_openai_realtime_tools", fake_openai_tools)

    async def fake_save(**kwargs):
        on_safe = kwargs.pop("on_safe", None)
        cap.saves.append(kwargs)
        if on_safe is not None:
            on_safe()
        return True

    patches.everywhere("save_voice_transcript", fake_save)

    def fake_create_task(task_type, operator="system", prompt="", result_data=None, **kw):
        n = len(cap.tasks) + 1
        cap.tasks.append({"type": getattr(task_type, "value", str(task_type)), "operator": operator,
                          "prompt": prompt, "result_data": result_data})
        return SimpleNamespace(task_id=f"task-{n}")

    patches.everywhere("create_task", fake_create_task)

    def fake_hybrid(vol_txt, query, k=3, operator="", **kw):
        return [f"[SNAP-20261002-000{i}] {query} hit {i} ({operator})" for i in range(1, int(k) + 1)]

    patches.everywhere("hybrid_retrieve", fake_hybrid)
    patches.everywhere("format_snapshot_for_delivery", lambda text: f"<<{text}>>")
    patches.everywhere("perform_web_fetch", lambda url, max_chars=80000: f"FETCHED {url} max={max_chars}")

    patches.setattr(chat_routes, "execute_get_media",
                    lambda url=None, task_id=None: {"url": url, "task_id": task_id, "kind": "image"})
    patches.setattr(chat_routes, "execute_list_media",
                    lambda media_type=None, limit=20: {"media_type": media_type, "limit": limit, "items": []})
    patches.setattr(chat_routes, "execute_search_media",
                    lambda query="", media_type=None, limit=10: {"query": query, "media_type": media_type,
                                                                  "limit": limit, "items": []})

    index = {
        "SNAP-20261001-0001": {"operator": OPERATOR, "byte_start": 0, "byte_end": 11},
        "SNAP-20261001-0002": {"operator": "someone-else", "byte_start": 11, "byte_end": 22},
        "SNAP-20261003-0001": {"operator": OPERATOR, "byte_start": 22, "byte_end": 33},
    }
    patches.setattr(fossils, "load_snapshot_index", lambda *a, **k: copy.deepcopy(index))
    patches.setattr(fossils, "read_volume_bytes", lambda path: b"first-snap second-snp third-snap")

    patches.setattr(contacts, "search_contacts",
                    lambda query, operator: [{"name": "Ann Example", "phone": "+15550000001",
                                              "query": query, "operator": operator}])
    patches.setattr(contacts, "upsert_contact",
                    lambda **kw: {"saved": True, **{k: v for k, v in kw.items()}})
    patches.setattr(contacts, "load_contacts",
                    lambda *a, **k: {OPERATOR: {"c1": {"name": "Ann Example"}}})
    patches.setattr(state, "get_operator_preference", lambda *a, **k: None)
    patches.setattr(tools_pkg, "BlackBoxToolExecutor", FakeExecutor)
    try:
        from Orchestrator.tools import blackbox_tools
        patches.setattr(blackbox_tools, "BlackBoxToolExecutor", FakeExecutor)
    except ImportError:
        pass
    patches.setattr(_datetime_mod, "datetime", FixedDatetime)
    patches.setattr(asyncio, "sleep", _fast_sleep)

    for registry in (models.GEMINI_LIVE_SESSIONS, models.GROK_LIVE_SESSIONS,
                     models.REALTIME_SESSIONS):
        for sid in [s for s in registry if s.startswith("golden-") or s.startswith("phone-golden")]:
            registry.pop(sid, None)


async def settle(*watched, quiet_polls: int = 6, timeout: float = 5.0) -> None:
    """Wait until nothing new is sent on any watched channel for a while
    (tool tasks, threads for retrieval and the listener all quiesce)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def size():
        total = 0
        for w in watched:
            if isinstance(w, list):
                total += len(w)
            elif hasattr(w, "sent"):
                total += len(w.sent)
            elif isinstance(w, Capture):
                total += len(w.saves) + sum(len(u.sent) for u in w.upstreams) + len(w.phone)
        return total

    last, quiet = size(), 0
    while quiet < quiet_polls and loop.time() < deadline:
        await _REAL_SLEEP(0.005)
        now = size()
        if now == last:
            quiet += 1
        else:
            last, quiet = now, 0


# =============================================================================
# Provider specs
# =============================================================================

def _spec(provider: str) -> SimpleNamespace:
    from Orchestrator import models
    if provider == "gemini":
        mod = importlib.import_module("Orchestrator.routes.gemini_live_routes")
        return SimpleNamespace(mod=mod, endpoint=mod.gemini_live_websocket,
                               registry=models.GEMINI_LIVE_SESSIONS)
    if provider == "grok":
        mod = importlib.import_module("Orchestrator.routes.grok_live_routes")
        return SimpleNamespace(mod=mod, endpoint=mod.grok_live_websocket,
                               registry=models.GROK_LIVE_SESSIONS)
    mod = importlib.import_module("Orchestrator.routes.realtime_routes")
    return SimpleNamespace(mod=mod, endpoint=mod.realtime_websocket,
                           registry=models.REALTIME_SESSIONS)


class Raw:
    """Script step: a raw (non-JSON) client frame."""

    def __init__(self, text: str):
        self.text = text


async def drive(provider: str, steps, cap: Capture, *, session_id: str,
                query: Optional[dict] = None) -> FakeClient:
    """Run the provider's WS endpoint with a fake client and feed it `steps`.

    A step is a client message (dict), a Raw frame, "hangup" (the client
    drops), or a callable(session) run in between (a coroutine is awaited).
    The bridge settles after every step, so frames land in causal order.
    """
    spec = _spec(provider)
    client = FakeClient(query)
    endpoint = asyncio.create_task(spec.endpoint(client, session_id))
    try:
        for step in steps:
            if isinstance(step, Raw):
                client._inbox.put_nowait(step.text)
            elif step == "hangup":
                client.hang_up()
            elif callable(step):
                out = step(spec.registry.get(session_id))
                if asyncio.iscoroutine(out):
                    await out
            else:
                client.feed(step)
            await settle(client, cap)
        await asyncio.wait_for(endpoint, timeout=10)
        await settle(client, cap)
    finally:
        if not endpoint.done():
            endpoint.cancel()
        spec.registry.pop(session_id, None)
    return client


# =============================================================================
# Scripts ("recorded" upstream sessions)
# =============================================================================

AUDIO_OUT = base64.b64encode(b"\x01\x00" * 8).decode()
AUDIO_IN = base64.b64encode(b"\x02\x00" * 8).decode()


def gemini_script() -> ScriptedUpstream:
    return ScriptedUpstream([
        (On("setup"), [{"setupComplete": {}},
                       {"sessionResumptionUpdate": {"newHandle": "resume-1", "resumable": True}}]),
        (On("clientContent"), [
            {"serverContent": {"outputTranscription": {"text": "Let me check. "}}},
            {"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": AUDIO_OUT}}]}}},
            {"toolCall": {"functionCalls": [
                {"id": "fc-1", "name": "search_contacts", "args": {"query": "Ann"}}]}},
        ]),
        (On("toolResponse"), [
            {"serverContent": {"outputTranscription": {"text": "Ann is your sister."}}},
            {"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": AUDIO_OUT}},
                {"text": "Ann is your sister."}]}}},
            {"serverContent": {"generationComplete": True}},
            {"serverContent": {"turnComplete": True}},
        ]),
        (On("realtime_input", nth=2), [
            {"serverContent": {"inputTranscription": {"text": "stop "}}},
            {"serverContent": {"outputTranscription": {"text": "Okay, I"}}},
            {"serverContent": {"interrupted": True}},
            {"serverContent": {"turnComplete": True}},
            {"toolCallCancellation": {"ids": ["fc-gone"]}},
            {"error": {"message": "golden upstream error"}},
        ]),
    ])


def openai_script(provider: str) -> ScriptedUpstream:
    first = [{"type": "session.updated", "session": {"tools": [{"name": "golden"}],
                                                      "instructions": "x"}}]
    if provider == "grok":
        first = [{"type": "conversation.created", "conversation": {"id": "conv-1"}}] + first
    text_delta = "response.text.delta" if provider == "grok" else "response.output_text.delta"
    return ScriptedUpstream([
        (On("session.update"), first),
        (On("response.create", nth=1), [
            {"type": "response.output_audio.delta", "delta": AUDIO_OUT},
            {"type": "response.output_audio_transcript.delta", "delta": "Let me check. "},
            {"type": text_delta, "delta": "(text)"},
            {"type": "response.function_call_arguments.done", "call_id": "call-1",
             "name": "search_contacts", "arguments": "{\"query\": \"Ann\"}"},
            {"type": "response.function_call_arguments.done", "call_id": "call-bad",
             "name": "search_contacts", "arguments": "{not json"},
            {"type": "response.output_audio.done"},
            {"type": "response.done", "response": {"status": "completed"}},
        ]),
        (On("conversation.item.create", nth=3), [
            {"type": "response.output_audio.delta", "delta": AUDIO_OUT},
            {"type": "response.output_audio_transcript.delta", "delta": "Ann is your sister."},
            {"type": "response.output_audio_transcript.done", "transcript": "Ann is your sister."},
            {"type": "response.done", "response": {"status": "completed"}},
        ]),
        (On("input_audio_buffer.append", nth=2), [
            {"type": "input_audio_buffer.speech_started"},
            {"type": "input_audio_buffer.speech_stopped"},
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "sto"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "stop"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Thank you."},
            {"type": "response.output_audio_transcript.delta", "delta": "Okay, I"},
        ]),
        (On("response.cancel"), [
            {"type": "response.done", "response": {"status": "cancelled"}},
            {"type": "golden.unknown_event"},
            {"type": "error", "error": {"message": "golden upstream error"}},
        ]),
    ])


SESSION_STEPS_COMMON = [
    {"type": "text_input", "text": "Who is Ann?"},
    {"type": "audio_input", "data": AUDIO_IN},
    {"type": "audio_input", "data": AUDIO_IN},
    {"type": "audio_commit"},
    {"type": "interrupt"},
    {"type": "video_frame", "data": "/9j/golden"},
    {"type": "video_frame", "data": "/9j/golden2"},
    {"type": "ping"},
    Raw("{this is not json"),
    {"type": "disconnect"},
]


def _connect(provider: str, **extra) -> dict:
    msg = {"type": "connect", "operator": OPERATOR}
    msg.update(extra)
    return msg


async def scenario_session(provider: str) -> dict:
    """Connect, a text turn with a tool call (and a malformed one), an audio
    turn that is interrupted, a video frame, a ping, then a clean disconnect."""
    patches, cap = Patches(), Capture()
    try:
        install(patches, cap)
        cap.pending = [gemini_script() if provider == "gemini" else openai_script(provider)]
        steps = [_connect(provider, voice={"gemini": "Puck", "grok": "Ara", "openai": "marin"}[provider])]
        steps += SESSION_STEPS_COMMON
        client = await drive(provider, steps, cap, session_id=f"golden-{provider}-session")
        return cap.result(client)
    finally:
        patches.undo()


async def scenario_reconnect(provider: str) -> dict:
    """The upstream drops mid-session (1011); the bridge reconnects on a new
    socket (resumption where the provider has it) and the session goes on."""
    patches, cap = Patches(), Capture()
    try:
        install(patches, cap)
        if provider == "gemini":
            first = ScriptedUpstream([(On("setup"), [
                {"setupComplete": {}},
                {"sessionResumptionUpdate": {"newHandle": "resume-7", "resumable": True}},
                {"serverContent": {"outputTranscription": {"text": "Half a rep"}}},
                Close(1011, "internal error")])])
            second = ScriptedUpstream([(On("setup"), [{"setupComplete": {}}])])
        else:
            opening = [{"type": "session.updated", "session": {}}]
            if provider == "grok":
                opening = [{"type": "conversation.created", "conversation": {"id": "conv-9"}}] + opening
            first = ScriptedUpstream([(On("session.update"), opening + [
                {"type": "response.output_audio_transcript.delta", "delta": "Half a rep"},
                Close(1011, "internal error")])])
            second = ScriptedUpstream([(On("session.update"), [{"type": "session.updated", "session": {}}])],
                                      on_open=([{"type": "session.updated", "session": {}}]
                                               if provider == "grok" else None))
        cap.pending = [first, second]
        steps = [_connect(provider), {"type": "text_input", "text": "after the blip"},
                 {"type": "disconnect"}]
        client = await drive(provider, steps, cap, session_id=f"golden-{provider}-reconnect")
        return cap.result(client)
    finally:
        patches.undo()


async def scenario_give_up(provider: str) -> dict:
    """The upstream drops and every redial fails: the bridge gives up after
    max_reconnects, saves the transcript and tells the client."""
    patches, cap = Patches(), Capture()
    try:
        install(patches, cap)
        if provider == "gemini":
            first = ScriptedUpstream([(On("setup"), [
                {"setupComplete": {}},
                {"serverContent": {"inputTranscription": {"text": "are you there"}}},
                {"serverContent": {"turnComplete": True}},
                Close(1011, "gone")])])
        else:
            first = ScriptedUpstream([(On("session.update"), [
                {"type": "conversation.item.input_audio_transcription.completed",
                 "transcript": "are you there"},
                Close(1011, "gone")])])
        cap.pending = [first] + [OSError("refused")] * 8
        steps = [_connect(provider)]
        client = await drive(provider, steps + ["hangup"], cap,
                                  session_id=f"golden-{provider}-giveup")
        return cap.result(client)
    finally:
        patches.undo()


async def scenario_connect_variants(provider: str) -> dict:
    """The connect-time payloads: custom role + greeting, the system operator,
    an unknown voice-agent preset, translate mode (Gemini/OpenAI), affective
    flags on a model that rejects them (Gemini)."""
    patches, cap = Patches(), Capture()
    results = {}
    try:
        install(patches, cap)
        variants = [
            ("custom_role", _connect(provider, role="You are calling the bakery.",
                                     greeting="Order two loaves.")),
            ("system", {"type": "connect", "operator": "system"}),
            ("preset_missing", _connect(provider, agent="no-such-preset")),
        ]
        if provider in ("gemini", "openai"):
            variants.append(("translate", _connect(provider, mode="translate", target_language="es")))
        if provider == "gemini":
            variants.append(("affective_rejected", _connect(provider, affective=True)))
            variants.append(("vad_thinking", _connect(provider, vad_sensitivity_start="HIGH",
                                                      vad_sensitivity_end="LOW",
                                                      thinking_level="low")))
        if provider == "openai":
            variants.append(("semantic_vad", _connect(provider, vad_type="semantic_vad",
                                                      vad_eagerness="high", noise_reduction="far_field",
                                                      transcription_delay="low",
                                                      interrupt_response=False)))
        if provider == "grok":
            variants.append(("asr_bias", _connect(provider, keyterms=["Zyx"], language_hint="en",
                                                  replace={"blak box": "BlackBox"},
                                                  reasoning_effort="high", voice="Rex")))
        for name, connect in variants:
            cap.dials, cap.upstreams, cap.saves, cap.tasks, cap.tool_groups = [], [], [], [], []
            FakeExecutor.calls = []
            cap.pending = [ScriptedUpstream()]
            client = await drive(provider, [connect, {"type": "disconnect"}], cap,
                                      session_id=f"golden-{provider}-{name}")
            results[name] = cap.result(client)
        return results
    finally:
        patches.undo()


# =============================================================================
# Tool ladder: every tool a voice model can call, per provider
# =============================================================================

TOOL_CALLS = [
    ("search_snapshots", {"query": "ann", "k": 2}),
    ("search_snapshots", {"query": ""}),
    ("web_fetch", {"url": "https://example.com/a", "max_chars": 500}),
    ("gemini_image", {"prompt": "a red barn at dusk", "numberOfImages": 2,
                      "reference_images": ["/ui/uploads/a.png"]}),
    ("openai_image", {"prompt": "a lighthouse", "size": "1024x1024"}),
    ("generate_video", {"prompt": "waves", "duration": 6, "image_url": "/ui/uploads/b.png",
                        "negativePrompt": "blur"}),
    ("generate_video", {"prompt": "x" * 120, "video_url": "/ui/uploads/c.mp4"}),
    ("lyria_music", {"prompt": "calm piano", "sampleCount": 2, "negativePrompt": "drums"}),
    ("lyria_music", {"prompt": "short"}),
    ("get_media", {"url": "/ui/uploads/a.png"}),
    ("list_media", {"media_type": "video"}),
    ("search_media", {"query": "sunset", "limit": 3}),
    ("send_sms", {"phone_number": "+15550000002", "message": "hi", "extra": "dropped"}),
    ("make_phone_call", {"phone_number": "+15550000003", "greeting": "hello"}),
    ("make_voice_call", {"phone_number": "+15550000004", "message": "msg"}),
    ("list_recent_snapshots", {"count": 9}),
    ("get_recent_snapshots", {}),
    ("search_contacts", {"query": "ann"}),
    ("save_contact", {"name": "Bo", "phone": "+15550000005", "tags": ["x"]}),
    ("create_cron_job", {"name": "daily", "schedule": "0 9 * * *"}),
    ("edit_cron_job", {"job_id": "j1"}),
    ("search_cron_jobs", {"query": "daily"}),
    ("use_computer", {"task": "open the browser"}),
    ("list_devices", {}),
    ("control_android_device", {"action": "home"}),
    ("get_current_time", {}),
    ("roll_dice", {"sides": 6}),
    ("silent_tool", {}),
]


async def scenario_tools(provider: str, system_operator: bool = False) -> dict:
    """Each tool call answered through the provider's tool path: the answer on
    the upstream socket, the client frames, executor calls and tasks."""
    from Orchestrator.toolvault.context import ToolResult
    from Orchestrator import models
    patches, cap = Patches(), Capture()
    try:
        install(patches, cap)
        FakeExecutor.answers = {
            "send_sms": ToolResult(success=True, result="SMS sent", data={"sid": "SM1"}),
            "create_cron_job": ToolResult(success=True, result="job created", data={"id": "j1"}),
            "use_computer": ToolResult(success=True, result="CU task started task_id=cu-1",
                                       data={"task_id": "cu-1"}),
            "silent_tool": ToolResult(success=True, result=""),
        }
        spec = _spec(provider)
        sid = f"golden-{provider}-tools"
        operator = "system" if system_operator else OPERATOR
        if provider == "gemini":
            session = models.GeminiLiveSession(session_id=sid, operator=operator)
        elif provider == "grok":
            session = models.GrokLiveSession(session_id=sid, operator=operator)
        else:
            session = models.RealtimeSession(session_id=sid, operator=operator)
        client = FakeClient()
        session.portal_ws = client
        upstream = ScriptedUpstream()
        ws_attr = {"gemini": "gemini_ws", "grok": "grok_ws", "openai": "openai_ws"}[provider]
        setattr(session, ws_attr, upstream)
        cap.upstreams.append(upstream)
        handler = {"gemini": "handle_gemini_message", "grok": "handle_grok_message",
                   "openai": "handle_openai_message"}[provider]
        handle = getattr(spec.mod, handler)
        for i, (name, args) in enumerate(TOOL_CALLS):
            if provider == "gemini":
                event = {"toolCall": {"functionCalls": [{"id": f"fc-{i}", "name": name, "args": args}]}}
            else:
                event = {"type": "response.function_call_arguments.done", "call_id": f"call-{i}",
                         "name": name, "arguments": json.dumps(args)}
            await handle(session, event)
            await settle(client, cap)
        return cap.result(client)
    finally:
        patches.undo()


# =============================================================================
# Phone bridge (Grok leg: the one whose caller audio works on the base code)
# =============================================================================

async def scenario_phone(provider: str) -> dict:
    from Orchestrator.phone.bridge import PhoneAIBridge
    from Orchestrator.phone.session import AIBackend, PhoneSession
    from Orchestrator.toolvault.context import ToolResult

    patches, cap = Patches(), Capture()
    try:
        install(patches, cap)
        FakeExecutor.answers = {"roll_dice": ToolResult(success=True, result="rolled 4")}
        backend = {"grok": AIBackend.GROK_LIVE, "gemini": AIBackend.GEMINI_LIVE,
                   "openai": AIBackend.OPENAI_REALTIME}[provider]
        if provider == "gemini":
            up = ScriptedUpstream([
                (On("setup"), [{"setupComplete": {}}]),
                (On("realtime_input", nth=1), [
                    {"serverContent": {"outputTranscription": {"text": "Hello caller."}}},
                    {"serverContent": {"modelTurn": {"parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": AUDIO_OUT}}]}}},
                    {"toolCall": {"functionCalls": [{"id": "pf-1", "name": "roll_dice",
                                                     "args": {"sides": 6}}]}},
                ]),
                (On("toolResponse"), [{"serverContent": {"turnComplete": True}}]),
            ])
        else:
            prefix = "response.output_audio" if provider == "grok" else "response.output_audio"
            up = ScriptedUpstream([
                (On("input_audio_buffer.append", nth=1), [
                    {"type": "conversation.item.input_audio_transcription.completed",
                     "transcript": "roll a die"},
                    {"type": "input_audio_buffer.committed"},
                    {"type": f"{prefix}.delta", "delta": AUDIO_OUT},
                    {"type": f"{prefix}_transcript.delta", "delta": "Rolling."},
                    {"type": f"{prefix}.done"},
                    {"type": f"{prefix}_transcript.done", "transcript": "Rolling."},
                    {"type": "response.function_call_arguments.done", "call_id": "pc-1",
                     "name": "roll_dice", "arguments": "{\"sides\": 6}"},
                    {"type": "response.done", "response": {"status": "completed"}},
                ]),
            ])
        cap.pending = [up]
        phone = PhoneSession(session_id="golden-call", ai_backend=backend)
        bridge = PhoneAIBridge(phone)

        async def on_pcm16(pcm: bytes, rate: int):
            cap.phone.append(["pcm16", len(pcm), rate, hashlib.sha1(pcm).hexdigest()[:12]])

        async def on_transcript(text: str):
            cap.phone.append(["transcript", text])

        bridge.on_ai_pcm16 = on_pcm16
        bridge.on_ai_transcript = on_transcript
        started = await bridge.start(operator=OPERATOR)
        await settle(cap)
        await bridge.send_pcm16(b"\x03\x00" * 160, 16000)
        await settle(cap)
        await bridge.stop()
        await settle(cap)
        return cap.result(None, {"started": started})
    finally:
        patches.undo()


SCENARIOS: Dict[str, Callable[[], Any]] = {}
for _p in ("gemini", "openai", "grok"):
    SCENARIOS[f"{_p}_session"] = (lambda p=_p: scenario_session(p))
    SCENARIOS[f"{_p}_reconnect"] = (lambda p=_p: scenario_reconnect(p))
    SCENARIOS[f"{_p}_connect_variants"] = (lambda p=_p: scenario_connect_variants(p))
    SCENARIOS[f"{_p}_tools"] = (lambda p=_p: scenario_tools(p))
    SCENARIOS[f"{_p}_tools_system"] = (lambda p=_p: scenario_tools(p, system_operator=True))
SCENARIOS["gemini_give_up"] = lambda: scenario_give_up("gemini")
SCENARIOS["phone_grok"] = lambda: scenario_phone("grok")


def run(name: str) -> dict:
    return asyncio.run(SCENARIOS[name]())


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def load_golden(name: str) -> dict:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def record(names=None) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name in names or SCENARIOS:
        data = run(name)
        golden_path(name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8")
        print(f"recorded {golden_path(name)}")


if __name__ == "__main__":
    os.environ.setdefault("BLACKBOX_TESTING", "1")
    record(sys.argv[1:] or None)
