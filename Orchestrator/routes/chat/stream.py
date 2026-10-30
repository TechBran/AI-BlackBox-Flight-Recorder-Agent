"""POST /chat/stream: the request resolver, the provider switch, the SSE
framing and the keepalive / off-loop context build.

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Optional, List

from Orchestrator.utils.operators import InvalidOperatorName
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse
from Orchestrator.checkpoint import app
from Orchestrator import location as user_location
from Orchestrator.telemetry_events import build_retrieval_activity
from Orchestrator.secret_redaction import redact
from Orchestrator.browser.dispatch import resolve_backend
from Orchestrator.onboarding import custom_servers
from Orchestrator import config as _config
from Orchestrator.wellness import access as wellness_access, turn as wellness_turn
from Orchestrator.wellness import mask as wellness_mask

from Orchestrator.routes.chat.context import (
    _checked_operator,
    _set_origin_device_id,
    _stash_turn_location,
    build_streaming_context,
    media_auto_route,
)
from Orchestrator.routes.chat.providers import (
    stream_anthropic_with_thinking,
    stream_custom_with_reasoning,
    stream_gemini_with_thinking,
    stream_openai_with_reasoning,
    stream_xai_with_reasoning,
)
from Orchestrator.routes.chat.cu import (
    stream_computer_use,
    stream_gemini_computer_use,
    stream_openai_computer_use,
)


# ─────────────────────────────────────────────────────────────────────────────
# M7 / WI-10 transport hardening: SSE keepalive during provider prefill.
#
# The M3 window audit (docs/plans/artifacts/2026-07-02-provider-window-audit.md)
# measured genuinely SILENT first-token waits through /chat/stream — the route
# emits stream_start then NOTHING until the provider's first event (anthropic
# 14.0s cold at 210k chars; historical band 30-60s under adaptive thinking).
# That silence trips idle-based client read timeouts (the 2026-04-25 stall).
#
# _stream_with_keepalive wraps every provider stream and yields a None sentinel
# for each KEEPALIVE_INTERVAL_S of provider silence; generate_sse renders the
# sentinel as an SSE COMMENT frame (": keepalive") — inert for compliant SSE
# parsers (Android M7.1a tolerates comment frames, test-pinned in 9b83714; the
# Portal parser skips blocks with no event:/data: lines). A comment frame is
# used instead of a named event so no client-side handler can misfire (the
# Portal already binds a CU-specific "heartbeat" event with step/total data).
#
# Gap-based rather than first-token-only: tool-loop FOLLOW-UP prefills (the
# same silence, after each tool_result) are covered too. While tokens flow,
# inter-event gaps are sub-second and no keepalive is ever emitted.
# ─────────────────────────────────────────────────────────────────────────────
KEEPALIVE_INTERVAL_S = 10.0
SSE_KEEPALIVE_FRAME = ": keepalive\n\n"


async def _stream_with_keepalive(stream, interval_s: float = KEEPALIVE_INTERVAL_S):
    """Relay provider events unchanged; yield a ``None`` sentinel whenever the
    next event is more than ``interval_s`` seconds away (the caller emits an
    SSE comment frame per sentinel). Provider exceptions propagate unchanged;
    the pending anext task is cancelled if the consumer closes early."""
    it = stream.__aiter__()
    while True:
        nxt = asyncio.ensure_future(it.__anext__())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(nxt), timeout=interval_s)
                    break
                except asyncio.TimeoutError:
                    # Provider still silent — heartbeat, keep awaiting the same task.
                    yield None
        except StopAsyncIteration:
            return
        finally:
            # Consumer disconnected / generator closed while the provider task
            # was still pending — don't leak the anext task.
            if not nxt.done():
                nxt.cancel()
        yield event


# Audit rank 45: retrieval (embed + hybrid search + rerank) is synchronous and
# takes ~31 s warm on MS02 when the reranker is on. Called inline from these
# async routes it froze the WHOLE event loop for that long - every other chat,
# voice socket and health probe stalled behind one reranking turn. It now runs
# in a worker thread (asyncio.to_thread copies contextvars, so the request-
# scoped origin device id still reaches it) INSIDE the SSE generator, and the
# same keepalive sentinels cover the wait so idle client read timeouts never
# trip while the context builds.
async def _streaming_context_off_loop(messages, operator, provider, window_guard_tokens,
                                      interval_s: Optional[float] = None):
    """Yield ``None`` keepalive sentinels while build_streaming_context runs in a
    worker thread, then yield its (context_messages, provenance, telemetry)
    tuple once. Exceptions from the build propagate unchanged. The interval is
    read at call time so tests can shrink KEEPALIVE_INTERVAL_S."""
    async def _once():
        yield await asyncio.to_thread(build_streaming_context, messages, operator, provider,
                                      window_guard_tokens=window_guard_tokens)
    interval = KEEPALIVE_INTERVAL_S if interval_s is None else interval_s
    async for item in _stream_with_keepalive(_once(), interval_s=interval):
        yield item


def _context_error_frames(route_tag: str, exc: Exception):
    """SSE frames for a context build that failed after the 200 was sent."""
    import traceback
    print(f"[{route_tag}] Context build failed: {exc}\n{traceback.format_exc()}")
    return [
        f"event: error\ndata: {json.dumps(f'Context build failed: {exc}')}\n\n",
        f"event: stream_end\ndata: {json.dumps({'status': 'error'})}\n\n",
    ]


def _error_detail_frame(event: dict) -> str:
    """The structured twin of an error event, as an ``error_detail`` SSE frame.

    W09: a classified provider failure (credits_exhausted) carries
    ``error_detail`` {kind, provider, reason, message} beside its plain-string
    data. The frame goes right BEFORE the ``error`` frame so a client can key
    on the kind; clients that don't know the event ignore it (Portal's switch
    has no default, Android logs unknown events), and the ``error`` frame
    itself is unchanged. "" when the event carries no detail.
    """
    detail = event.get("error_detail")
    if not isinstance(detail, dict):
        return ""
    return f"event: error_detail\ndata: {json.dumps(detail)}\n\n"


# There is no GET /chat/stream (audit M3 U20): it ran a whole tool-executing
# turn from query parameters, so a cross-site <img src=...> could start one.
# Every client (Portal, Android, cron) POSTs.


# The providers _open_provider_stream runs by their own name. Every other
# string (google, gemini, a mis-cased or an unknown one: "local", "claude",
# "custom:<alias>") runs Gemini. dispatched_provider() is that rule, and the
# switch and the turn's WellnessTurn both read it, so the health-data gate
# decides on the AI that will read the answer, never on the client's spelling.
_NAMED_STREAM_PROVIDERS = frozenset({"openai", "anthropic", "xai", "custom", "computer-use"})


def dispatched_provider(provider) -> str:
    """The provider a /chat/stream turn for ``provider`` runs on."""
    return provider if provider in _NAMED_STREAM_PROVIDERS else "google"


@dataclass
class StreamRequest:
    """A POST /chat/stream body resolved into what the turn runs with."""
    messages: list
    provider: str
    model: str
    operator: str
    guard: Optional[int]          # window-guard tokens (custom servers); None = provider table
    cu_session_id: Optional[str]  # CU session ID for reconnection/queuing
    cu_device_id: str             # CU target device on the Tailscale mesh
    location: Optional[dict]      # M1 per-turn location ride-along


def _resolve_stream_request(body: dict, signed=None):
    """The StreamRequest for a POST /chat/stream body, or the 400 JSONResponse.

    Stamps the request-scoped origin device (M3) and the turn's carried
    location (M1) as it goes, in the order the route always did, and last
    the turn's WellnessTurn (M7), once the media auto-route and the model
    resolution have settled who will read the answer. ``signed`` is the
    request's verified X-BlackBox-Origin (the enrolled phone's device key;
    wellness_access.origin_from_request), the only credential a turn holds."""
    messages = body.get("messages", [])
    provider = body.get("provider") or _config.DEFAULT_PROVIDER
    model = body.get("model")
    try:
        operator = _checked_operator(body.get("operator"))
    except InvalidOperatorName as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid operator: {e}"})
    cu_session_id = body.get("session_id")  # CU session ID for reconnection/queuing
    cu_device_id = body.get("device_id", "blackbox")  # CU target device on Tailscale mesh
    # M3: originating device (Android app self-reports its tailnet IPv4). Absent for
    # Portal/MCP → None → operator's primary-device fallback. Request-scoped so it
    # threads into every tool executor built during this turn.
    _set_origin_device_id(body.get("origin_device_id"))
    # M1: optional per-turn location object {lat, lon, city, state} sent by
    # the Android app alongside the prompt. Absent (Portal/MCP/cron) -> None
    # -> perfect no-op. Untrusted input: normalize() rejects anything junk.
    turn_location = None
    try:
        turn_location = user_location.normalize(body.get("location"))
    except Exception:
        turn_location = None
    _stash_turn_location(operator, turn_location)

    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages required"})

    # The media auto-route every chat path shares (media_auto_route): an
    # audio/video turn moves to Google BEFORE the context build, so the
    # window guard and the default model follow the provider that runs. A
    # computer-use turn drives a desktop session and is never re-routed.
    if provider != "computer-use":
        provider, media_switched = media_auto_route(messages, provider)
        if media_switched:
            model = None  # the requested id belongs to the original provider

    # Default models with thinking support
    guard = None  # non-custom providers use context_builder's provider window table
    if provider == "custom":
        # Resolve the server BEFORE build_streaming_context (order matters:
        # its context window sizes the window guard). Empty/Auto model ->
        # the server's first discovered model, alias-qualified; unresolved
        # server / no discovered models stay as-is — stream_custom_with_reasoning
        # emits the clean error event. The guard budgets from the FINAL
        # dispatched bare model (per-model model_context override on
        # multi-model llama-swap hosts), falling back to context_tokens.
        _srv, _bare = custom_servers.resolve_model(model or "")
        if _srv is not None and (not model or _bare.strip().lower() == "auto"):
            _discovered = _srv.get("last_models") or []
            if _discovered:
                _bare = _discovered[0]
                model = custom_servers.qualify(_srv.get("alias", ""), _bare)
        guard = custom_servers.window_guard_tokens(_srv, _bare)
    else:
        # Empty/"Auto" -> the provider's configured *_MODEL_DEFAULT (the same
        # resolver /chat, cron and SMS use); a retired Claude id -> its
        # successor. An unknown provider falls through to Gemini below.
        model = _config.resolve_default_model(provider, model)
    # M7: the turn's health-data context, request-scoped like the origin device
    # (the context build's and the tool loop's threads copy it). It carries the
    # turn key the delivered rows and the mask join on (turn.current()), and
    # names the provider that will run, not the one the client spelled.
    wellness_turn.stamp(wellness_access.request_turn(
        operator=operator, provider=dispatched_provider(provider), model=model, signed=signed,
        origin_device_id=body.get("origin_device_id"),
        declared=body.get(wellness_access.DECLARED_KEY)))
    return StreamRequest(messages=messages, provider=provider, model=model, operator=operator,
                         guard=guard, cu_session_id=cu_session_id, cu_device_id=cu_device_id,
                         location=turn_location)


def _wellness_prepare(req: StreamRequest, context_messages: list):
    """(messages for the provider, the turn key for stream_start), the M7
    ledger rule's two stream-side steps, in one worker-thread hop (both read
    the operator's health store).

    The history scrub (A3b): the history and the provider are the client's,
    so earlier health bubbles never reach a provider the operator did not
    allow (a mid-chat model switch); the same list when nothing changes. The
    durable carry: for an operator with a health store, stream_start names the
    turn key (`wellness_turn`) the client echoes to /chat/save, so the save
    masks exactly this turn's health results, even after a restart."""
    messages = wellness_mask.scrub_history(context_messages, req.operator, req.provider, req.model)
    return messages, wellness_mask.stream_turn_key(req.operator)


def _stream_start_payload(req: StreamRequest, provenance, wellness_key=None) -> dict:
    """The stream_start event (`wellness_turn` only for a stamped turn of an
    operator with a health store: byte-identical for everyone else)."""
    payload = {'provider': req.provider, 'model': req.model, 'provenance': provenance}
    if wellness_key:
        payload["wellness_turn"] = wellness_key
    return payload


def _open_provider_stream(req: StreamRequest, context_messages: list):
    """The provider event stream for one /chat/stream turn (dispatched_provider's rule)."""
    provider, model, operator = dispatched_provider(req.provider), req.model, req.operator
    if provider == "openai":
        return stream_openai_with_reasoning(context_messages, model, operator=operator)
    if provider == "anthropic":
        return stream_anthropic_with_thinking(context_messages, model, operator=operator)
    if provider == "xai":
        return stream_xai_with_reasoning(context_messages, model, operator=operator)
    if provider == "custom":
        return stream_custom_with_reasoning(context_messages, model, operator=operator)
    if provider == "computer-use":
        backend = resolve_backend(model)
        if backend == "google":
            return stream_gemini_computer_use(context_messages, model, operator=operator,
                                              session_id=req.cu_session_id, device_id=req.cu_device_id)
        if backend == "openai":
            return stream_openai_computer_use(context_messages, model, operator=operator,
                                              session_id=req.cu_session_id, device_id=req.cu_device_id)
        return stream_computer_use(context_messages, model, operator=operator,
                                   session_id=req.cu_session_id, device_id=req.cu_device_id)
    return stream_gemini_with_thinking(context_messages, model, operator=operator)


def _sse_frames(event: dict) -> List[str]:
    """The SSE frames for one provider event, as the client receives them.

    Every provider's error text funnels here: secrets are masked (exception
    text can embed a request URL) before the client, and a classified failure
    (W09: credits_exhausted) gets its structured error_detail frame right
    before the error frame."""
    event_type = event.get("type", "unknown")
    event_data = event.get("data", "")
    frames = []
    if event_type == "error":
        event_data = redact(event_data)
        detail_frame = _error_detail_frame(event)
        if detail_frame:
            frames.append(detail_frame)
    if event_type == "image_task":
        print(f"[SSE] Converting image_task event to SSE format: {event_data}")
    frames.append(f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n")
    return frames


@app.post("/chat/stream")
async def chat_stream_post(request: Request):
    """POST version of streaming chat for larger payloads.
    Body: {messages, provider, model, operator}
    """
    try:
        body = await request.json()
        req = _resolve_stream_request(body, await wellness_access.origin_from_request(request))
        if isinstance(req, JSONResponse):
            return req

        async def generate_sse():
            # Context build off the event loop with keepalives (rank 45; see
            # _streaming_context_off_loop).
            ctx = None
            try:
                async for item in _streaming_context_off_loop(req.messages, req.operator, req.provider, req.guard):
                    if item is None:
                        yield SSE_KEEPALIVE_FRAME
                        continue
                    ctx = item
            except Exception as e:
                for frame in _context_error_frames("STREAM POST", e):
                    yield frame
                return
            context_messages, provenance, telemetry = ctx
            telemetry["model"] = req.model

            # M1: location rides along inside the user prompt block. Applied
            # after context building so retrieval/tool-injection queries stay
            # byte-identical; only what the model reads changes.
            context_messages = user_location.apply_location_to_messages(context_messages, req.location)

            # M7 ledger rule: the history scrub and the durable-carry key
            # (see _wellness_prepare), off the loop.
            context_messages, wellness_key = await asyncio.to_thread(
                _wellness_prepare, req, context_messages)

            yield f"event: stream_start\ndata: {json.dumps(_stream_start_payload(req, provenance, wellness_key))}\n\n"

            # The Signal (Task 1.3): burst the honest retrieval telemetry as
            # UI-only `system_activity` SSE frames, immediately after stream_start
            # and before the provider's first token. These are PRESENTATION-ONLY
            # — serialized straight to the wire, never added to the
            # prompt/context/messages or any snapshot. Honest degradation: a
            # non-retrieval / empty-corpus turn yields [] and nothing is emitted.
            for _sa in build_retrieval_activity(telemetry):
                yield f"event: {_sa['type']}\ndata: {json.dumps(_sa['data'])}\n\n"

            async for event in _stream_with_keepalive(_open_provider_stream(req, context_messages)):
                if event is None:
                    # M7: provider prefill keepalive — inert SSE comment frame.
                    yield SSE_KEEPALIVE_FRAME
                    continue
                for frame in _sse_frames(event):
                    yield frame

            yield f"event: stream_end\ndata: {json.dumps({'status': 'complete'})}\n\n"

        return StreamingResponse(
            generate_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        import traceback
        print(f"[STREAM POST] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})
