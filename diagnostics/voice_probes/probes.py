"""Live WS probes — network + paid API calls.

Invoked by the run.py CLI and the probe_live pytest suite; NEVER imported by
the service. Failure capture: WS close code/reason (Gemini setup rejections
arrive as close 1007/1008), HTTP status at upgrade (OpenAI unknown-model
rejections), error events, timeouts.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional, Sequence

import websockets

from diagnostics.voice_probes.env import get_key
from diagnostics.voice_probes.harness import (
    ProbeResult,
    build_gemini_url,
    build_openai_url,
    build_xai_url,
    classify_first_event,
    now_iso,
    redact_text,
)

# Mirrors the box's connect idiom (Orchestrator/routes/realtime_routes.py:282-289).
CONNECT_KW = dict(open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10)


async def _recv_json(ws, timeout: float) -> Dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def _capture_failure(result: ProbeResult, exc: BaseException) -> None:
    if isinstance(exc, websockets.exceptions.ConnectionClosed):
        frame = exc.rcvd
        result.close_code = frame.code if frame else None
        result.close_reason = redact_text(frame.reason if frame else "")
        result.error = "connection closed"
    elif isinstance(exc, websockets.exceptions.InvalidStatus):
        body = ""
        try:
            body = exc.response.body.decode("utf-8", "replace")[:500]
        except Exception:
            pass
        result.error = redact_text(f"HTTP {exc.response.status_code} at WS upgrade: {body}")
    elif isinstance(exc, asyncio.TimeoutError):
        result.error = "timeout waiting for server event"
    else:
        result.error = redact_text(f"{type(exc).__name__}: {exc}")


async def _probe_openai_style(
    provider: str,
    url: str,
    key_name: str,
    model: str,
    probe: str,
    session_patch: Optional[Dict],
    audio_pcm: Optional[bytes],
    listen_s: float,
    timeout: float,
) -> ProbeResult:
    result = ProbeResult(provider=provider, model=model, probe=probe, ts=now_iso())
    key = get_key(key_name)
    if not key:
        result.error = f"{key_name} not set in service env (.env)"
        return result
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {key}"}, **CONNECT_KW
        ) as ws:
            first = await _recv_json(ws, timeout)
            result.add_event(first)
            result.ok, result.resolved_model = classify_first_event(provider, first)
            if session_patch is not None and result.ok:
                await ws.send(json.dumps({"type": "session.update", "session": session_patch}))
                for _ in range(10):
                    event = await _recv_json(ws, timeout)
                    result.add_event(event)
                    if event.get("type") in ("session.updated", "error"):
                        result.ok = event.get("type") == "session.updated"
                        break
            if audio_pcm and result.ok:
                # ~100ms chunks of 24kHz s16le mono, paced like a live mic.
                for i in range(0, len(audio_pcm), 4800):
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio_pcm[i:i + 4800]).decode(),
                    }))
                    await asyncio.sleep(0.02)
            if listen_s > 0 and result.ok:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + listen_s
                while loop.time() < deadline:
                    try:
                        event = await _recv_json(ws, deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    result.add_event(event)
        types = sorted({e.get("type", "?") for e in result.events if isinstance(e, dict)})
        result.notes = (result.notes + " " if result.notes else "") + \
            "event_types=" + ",".join(types)
    except Exception as exc:
        _capture_failure(result, exc)
    return result


async def probe_openai(
    model: str, *, probe: str = "handshake",
    session_patch: Optional[Dict] = None, timeout: float = 15.0,
) -> ProbeResult:
    return await _probe_openai_style(
        "openai", build_openai_url(model), "OPENAI_API_KEY",
        model, probe, session_patch, None, 0.0, timeout,
    )


async def probe_xai(
    model: str = "", *, probe: str = "handshake",
    session_patch: Optional[Dict] = None,
    audio_pcm: Optional[bytes] = None, listen_s: float = 0.0,
    timeout: float = 15.0,
) -> ProbeResult:
    return await _probe_openai_style(
        "xai", build_xai_url(model), "XAI_API_KEY",
        model, probe, session_patch, audio_pcm, listen_s, timeout,
    )


async def probe_gemini(
    model: str, *, probe: str = "handshake",
    tools: Optional[List[Dict]] = None,
    api_version: str = "v1beta",
    setup_extra: Optional[Dict] = None,
    response_modalities: Optional[Sequence[str]] = ("AUDIO",),
    timeout: float = 20.0,
) -> ProbeResult:
    """Send BidiGenerateContentSetup; ok iff setupComplete arrives.

    Setup shape mirrors Orchestrator/routes/gemini_live_routes.py:429-448.
    response_modalities=None omits generationConfig entirely (server default —
    used by the translate-shape probe).
    """
    result = ProbeResult(provider="gemini", model=model, probe=probe, ts=now_iso())
    key = get_key("GOOGLE_API_KEY")
    if not key:
        result.error = "GOOGLE_API_KEY not set in service env (.env)"
        return result
    setup: Dict[str, Any] = {"model": f"models/{model}"}
    if response_modalities is not None:
        setup["generationConfig"] = {"responseModalities": list(response_modalities)}
    if tools is not None:
        setup["tools"] = tools
        n = sum(len(t.get("functionDeclarations", []))
                for t in tools if isinstance(t, dict))
        result.notes = f"{n} functionDeclarations"
    if setup_extra:
        setup.update(setup_extra)
    try:
        async with websockets.connect(build_gemini_url(api_version, key), **CONNECT_KW) as ws:
            await ws.send(json.dumps({"setup": setup}))
            first = await _recv_json(ws, timeout)
            result.add_event(first)
            result.ok, _ = classify_first_event("gemini", first)
    except Exception as exc:
        _capture_failure(result, exc)
    return result
