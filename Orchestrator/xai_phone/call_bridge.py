"""Attach an inbound xAI SIP call to a GrokLiveSession.

A verified `realtime.call.incoming` webhook (routes/xai_phone_routes.py) hands
us a call_id; opening wss://api.x.ai/v1/realtime?call_id={call_id} attaches
this process as the call's agent. AUDIO FLOWS xAI-SIDE: the caller's SIP leg
is the audio path — there is NO local audio pump (unlike phone/bridge.py's
Asterisk leg). We drive only session config, tool dispatch and transcripts
through the existing grok_live_routes machinery, with portal_ws left None —
every portal send goes through _safe_ws_send, which no-ops on None. This is
the same drive-without-a-portal-WS shape as phone/bridge.py:1863-1932
(_start_grok), which reuses connect_to_grok + configure_grok_session.

Reaper safety (live_session_reaper.py:48-66): while the call is live the
session keeps status="connected" and is NEVER reaped (the invariant protects
sessions without a portal_ws); _finalize_call flips it to "disconnected" and
stamps last_activity, so the reaper evicts it after the grace window.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from Orchestrator.live_session_reaper import release_payload
from Orchestrator.models import GROK_LIVE_SESSIONS, GrokLiveSession
from Orchestrator.volume import now_utc_iso
from Orchestrator.xai_phone import provisioning

logger = logging.getLogger(__name__)

# The inbound line is a system surface (outbound-call precedent in
# configure_grok_session's is_system_operator branch) unless the preset says otherwise.
DEFAULT_OPERATOR = "system"

_ACTIVE_STATUSES = ("connecting", "connected", "responding")


def _resolve_default_preset() -> dict:
    """Line default preset: default_preset_id in credentials/xai_phone.json
    -> P4 voice-agent preset registry.

    Guarded import: P5 keeps working if P4's registry is absent (fresh box,
    partial deploy) — falls back to empty defaults. NOTE: adjust the import
    below if P4 landed its registry under a different module path.
    """
    preset_id = provisioning.get_default_preset_id()
    if not preset_id:
        return {}
    try:
        from Orchestrator.voice_agents.registry import get_preset  # P4 module
    except ImportError:
        logger.warning("[XAI-PHONE] voice-agent preset registry unavailable; using defaults")
        return {}
    preset = get_preset(preset_id)
    if not preset:
        logger.warning("[XAI-PHONE] default_preset_id %r not found; using defaults", preset_id)
        return {}
    return preset


async def attach_call(call_id: str, payload: Optional[dict] = None) -> Optional[str]:
    """Attach to an incoming xAI SIP call. Returns the session_id, or None on failure."""
    # Late imports: grok_live_routes imports half the Orchestrator — keep this
    # module cheap to import for the webhook route and for tests.
    from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE
    from Orchestrator.routes.grok_live_routes import (
        configure_grok_session,
        connect_to_grok,
        grok_keepalive_loop,
        grok_listener,
        save_grok_session_to_blackbox,
    )

    session_id = f"phone-xai-{call_id}"
    existing = GROK_LIVE_SESSIONS.get(session_id)
    if existing and existing.status in _ACTIVE_STATUSES:
        logger.warning("[XAI-PHONE] duplicate incoming webhook for %s — ignoring", call_id)
        return session_id

    preset = _resolve_default_preset()
    session = GrokLiveSession(
        session_id=session_id,
        operator=preset.get("created_by") or DEFAULT_OPERATOR,
        status="connecting",
        created_at=now_utc_iso(),
        call_id=call_id,
    )
    GROK_LIVE_SESSIONS[session_id] = session

    if not await connect_to_grok(session, call_id=call_id):
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # reaper evicts after grace
        logger.error("[XAI-PHONE] failed to attach to call %s", call_id)
        return None

    await configure_grok_session(
        session,
        session.operator,
        voice=preset.get("voice") or GROK_LIVE_DEFAULT_VOICE,
        custom_role=preset.get("instructions") or "",
    )

    listener_task = asyncio.create_task(grok_listener(session))
    keepalive_task = asyncio.create_task(grok_keepalive_loop(session))
    asyncio.create_task(
        _finalize_call(session, listener_task, keepalive_task, save_grok_session_to_blackbox)
    )
    logger.info("[XAI-PHONE] attached to call %s as session %s", call_id, session_id)
    return session_id


async def _finalize_call(session, listener_task, keepalive_task, save_fn) -> None:
    """Teardown when xAI closes the call WS (hangup/transfer/drop).

    Mirrors the portal-WS finally block (grok_live_routes.py:1440-1471):
    save transcript (P1b /chat/save path), close, mark disconnected, stamp
    last_activity (starts the reaper grace clock), release buffers.
    """
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
    finally:
        # A hung-up call_id is dead — suppress reconnect churn from the
        # listener's close handler / keepalive stale detection.
        session.intentional_disconnect = True
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        try:
            await save_fn(session)
        except Exception as e:
            logger.error("[XAI-PHONE] transcript save failed for %s: %s", session.session_id, e)
        if session.grok_ws:
            try:
                await session.grok_ws.close()
            except Exception:
                pass
            session.grok_ws = None
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # start reaper grace clock
        release_payload(session)
        logger.info("[XAI-PHONE] call session %s finalized", session.session_id)
