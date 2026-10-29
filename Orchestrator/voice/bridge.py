"""LiveVoiceBridge: the one session state machine of the live-voice routes.

Portal / Android  <--WS-->  LiveVoiceBridge  <--WS-->  provider (Gemini Live,
OpenAI Realtime, xAI Grok)

The bridge owns what every provider shares: the client WebSocket loop (idle
timeout, per-message isolation, connect / disconnect / ping), the connect
sequence, reconnect with exponential backoff, the keepalive, the upstream
listener and the teardown. A VoiceAdapter supplies what differs: how to dial
and configure the provider, how client messages map onto provider frames and
provider events onto client frames, the keepalive frame, and the transcript
save.

The state machine keeps the live-voice fixes of audit ranks 50/51 in ONE place:
liveness is the transport's ping/pong (no stale-timer reconnects), an attach
generation stamped by claim_session keeps a superseded handler or reconnect
off a re-attached session, tool calls run as session tasks, and a dead session
closes its client socket instead of answering pings forever.

Adapters reach their route module's entry points (``gemini_reconnect``,
``grok_listener``, ``save_session_to_blackbox`` ...) by name at call time,
so the phone bridge, the xAI SIP call bridge, the reaper and the tests that
call or swap one of them drive this same machine.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

import websockets
import websockets.exceptions
from fastapi import WebSocketDisconnect

from Orchestrator.routes.voice_ws_shared import (
    claim_session,
    close_portal_ws,
    close_upstream,
    end_portal_session,
    reset_for_connect,
    retire_upstream,
    safe_send,
    spawn_reconnect,
    superseded,
)
from Orchestrator.volume import now_utc_iso

# How long the client WS may stay silent before the session is dropped: a
# suspended or dead Android client stops sending even its pings.
CLIENT_IDLE_TIMEOUT_S = 120.0


class VoiceAdapter:
    """What one provider contributes to the shared bridge.

    Class attributes name the provider; the methods are the provider's wire
    vocabulary. Every hook that a route exposes as a module-level function is
    called through that function, never re-implemented here.
    """

    #: Client-visible provider name ("Gemini connection closed").
    name = ""
    #: Log prefix ("[GEMINI-LIVE]").
    log_prefix = ""
    #: The session attribute that holds the upstream socket ("gemini_ws").
    ws_attr = ""
    #: Config key the client is told is missing.
    key_name = ""
    #: Client status while dialing, and the error when the dial fails.
    connecting_status = ""
    connect_failed = ""
    #: Seconds between keepalive frames (an idle provider times the session out).
    keepalive_interval = 15
    # Terminal behaviour (P1.7). Gemini forwards an unexpected upstream close
    # to the client, says nothing when its OLD socket closes mid-reconnect, and
    # closes the client WS when the session is dead for good.
    forward_close_frames = False
    quiet_reconnecting_close = False
    terminal_close = False

    # --- session store ---
    def sessions(self) -> Dict[str, Any]:
        raise NotImplementedError

    def new_session(self, session_id: str):
        raise NotImplementedError

    # --- availability ---
    def websockets_available(self) -> bool:
        raise NotImplementedError

    def api_key(self) -> str:
        raise NotImplementedError

    # --- client connect message ---
    def url_params(self, websocket) -> Dict[str, Any]:
        """Connect parameters the Android client passes in the URL query."""
        return {}

    async def parse_connect(self, session, websocket, data: Dict, url: Dict) -> Optional[Dict]:
        """Resolve a connect message into the params dial/configure use.

        May send a warning or an error to the client; None drops the message
        (the session is not dialed).
        """
        raise NotImplementedError

    async def dial(self, session, params: Dict) -> bool:
        raise NotImplementedError

    async def configure(self, session, params: Dict) -> None:
        raise NotImplementedError

    async def inject_greeting(self, session, greeting: str) -> None:
        """Make the model speak first (an outbound call's greeting)."""

    def connected_data(self, session, session_id: str, params: Dict) -> Dict:
        raise NotImplementedError

    # --- traffic ---
    async def handle_client(self, session, data: Dict) -> None:
        raise NotImplementedError

    async def handle_event(self, session, event: Dict) -> None:
        raise NotImplementedError

    async def answer_failed_event(self, session, event: Dict, exc: BaseException) -> None:
        """Answer the tool calls of an event whose handling raised, so no call
        id is left dangling (the model would wait forever: dead air)."""

    def on_frame(self, session) -> None:
        """A frame arrived on the upstream (a provider with no setup ack resets
        its reconnect budget here: the socket is proven healthy)."""

    def keepalive_frame(self, session) -> Optional[Dict]:
        raise NotImplementedError

    # --- reconnect ---
    def flush(self, session) -> None:
        """Move a reply still in the buffers into the conversation (its socket
        is going away and will never complete it)."""

    async def redial(self, session) -> bool:
        raise NotImplementedError

    async def reconfigure(self, session) -> bool:
        """Configure the redialed session; False when the provider resumed the
        conversation server-side and nothing was rebuilt."""
        raise NotImplementedError

    # --- transcript ---
    async def save(self, session) -> None:
        raise NotImplementedError

    # --- the route's entry points, looked up at call time ---
    def reconnect(self, session):
        raise NotImplementedError

    def listener(self, session):
        raise NotImplementedError

    def keepalive(self, session):
        raise NotImplementedError


class LiveVoiceBridge:
    """The shared state machine, bound to one provider's adapter."""

    def __init__(self, adapter: VoiceAdapter):
        self.adapter = adapter

    # =========================================================================
    # Client WebSocket endpoint
    # =========================================================================

    async def serve(self, websocket, session_id: str) -> None:
        """Bridge one client WebSocket to the provider.

        1. The client connects and sends 'connect' (operator, voice, options).
        2. The bridge dials the provider and configures the session (tools,
           context, voice).
        3. Messages flow both ways; tool calls run here and are answered.
        A client re-attaching to the same session id (network blip) takes the
        session over; the older handler's teardown then leaves it alone.
        """
        a = self.adapter
        p = a.log_prefix
        print(f"{p} WebSocket connection request for session: {session_id}")
        await websocket.accept()
        print(f"{p} WebSocket accepted for session: {session_id}")

        # Android passes its options in the URL query instead of the connect
        # message; the JSON connect message wins, the URL fills what it omits.
        url = a.url_params(websocket)

        if not a.websockets_available():
            await safe_send(websocket, {
                "type": "error",
                "data": "Server missing 'websockets' library. Install with: pip install websockets"
            })
            await websocket.close()
            return

        if not a.api_key():
            await safe_send(websocket, {
                "type": "error",
                "data": f"{a.key_name} not configured on server"
            })
            await websocket.close()
            return

        sessions = a.sessions()
        session = sessions.get(session_id)
        if not session:
            session = a.new_session(session_id)
            sessions[session_id] = session

        session.portal_ws = websocket
        session.last_activity = now_utc_iso()
        # This handler's attach: a later re-attach of the same session id bumps
        # it, and from then on the session is that handler's.
        generation = claim_session(session)

        keepalive_task = None

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(),
                                                 timeout=CLIENT_IDLE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    print(f"{p} Client idle timeout ({CLIENT_IDLE_TIMEOUT_S:.0f}s): {session_id}")
                    await safe_send(websocket, {"type": "error", "data": "Idle timeout"})
                    break

                # One bad message does not kill the session.
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as e:
                    print(f"{p} Bad message from client: {e}")
                    continue

                msg_type = data.get("type", "")

                if msg_type == "connect":
                    started = await self._connect(session, websocket, session_id, data, url)
                    if started is not None:
                        keepalive_task = started

                elif msg_type == "disconnect":
                    if not superseded(session, generation):
                        session.intentional_disconnect = True
                    break

                elif msg_type == "ping":
                    await safe_send(websocket, {"type": "pong"})

                else:
                    try:
                        await a.handle_client(session, data)
                    except Exception as handler_err:
                        print(f"{p} Handler error (non-fatal): {handler_err}")

        except WebSocketDisconnect:
            print(f"{p} Portal WebSocket disconnected: {session_id}")
            if session.portal_ws is websocket:
                # Keep the provider connection through the reaper's grace window.
                session.portal_ws = None

        except Exception as e:
            print(f"{p} WebSocket error: {e}")
            await safe_send(websocket, {"type": "error", "data": str(e)})

        finally:
            await self.end_portal_session(session, websocket, generation, keepalive_task)

    async def _connect(self, session, websocket, session_id: str, data: Dict, url: Dict):
        """A connect message: dial and configure. Returns the keepalive task
        it started, or None when the session was not (re)connected."""
        a = self.adapter
        params = await a.parse_connect(session, websocket, data, url)
        if params is None:
            return None

        await safe_send(websocket, {"type": "status", "data": a.connecting_status})

        # A re-attach (or a second connect on this socket) starts clean: retire
        # whatever the previous connect left live, then drop the previous
        # handler's teardown flags. Without the reset the keepalive died on its
        # first tick, every upstream close read as deliberate, goAway was
        # ignored and the reconnect budget carried over
        # (O09-live-voice-providers-01).
        await retire_upstream(session, a.ws_attr)
        a.flush(session)  # the retired upstream's reply ends here
        reset_for_connect(session)

        if not await a.dial(session, params):
            await safe_send(websocket, {"type": "error", "data": a.connect_failed})
            return None

        await a.configure(session, params)

        # Provenance once per session start: the client shows which snapshots
        # seeded the system message.
        if session.provenance:
            await safe_send(websocket, {"type": "provenance", "data": session.provenance})

        # The listener lives on the SESSION: a reconnect respawns it, so a local
        # here would go stale after the first reconnect.
        session.listener_task = asyncio.create_task(a.listener(session))
        keepalive_task = asyncio.create_task(a.keepalive(session))
        session.keepalive_task = keepalive_task

        greeting = params.get("greeting") or ""
        if greeting:
            # Outbound call: the model speaks first when the callee answers.
            print(f"{a.log_prefix} Injecting outbound greeting: {greeting[:80]}...")
            try:
                await a.inject_greeting(session, greeting)
            except Exception as e:
                print(f"{a.log_prefix} Greeting injection failed: {e}")

        await safe_send(websocket, {
            "type": "connected",
            "data": a.connected_data(session, session_id, params),
        })
        return keepalive_task

    async def end_portal_session(self, session, websocket, generation: int,
                                 keepalive_task) -> None:
        """Teardown when a handler's client WS ends (voice_ws_shared.end_portal_session).

        Only the CURRENT owner tears the session down, and it stops touching the
        session the moment a re-attach claims it, even mid-teardown
        (O09-live-voice-providers-01).
        """
        a = self.adapter
        await end_portal_session(session, generation, keepalive_task, ws_attr=a.ws_attr,
                                 save=a.save, log_prefix=a.log_prefix, flush=a.flush)

    # =========================================================================
    # Reconnect
    # =========================================================================

    async def reconnect(self, session) -> None:
        """Re-dial the provider after an unexpected close, with backoff.

        Runs detached (spawn_reconnect), so every await is followed by a check
        that the session is still this attach's and still wanted.
        """
        a = self.adapter
        p = a.log_prefix
        if session.is_reconnecting:
            print(f"{p} Already reconnecting, skipping")
            return

        # The attach this reconnect works for. A client re-attach resets
        # intentional_disconnect, so after every await the generation is what
        # says whether the session is still ours to re-dial.
        generation = session.generation

        if session.reconnect_count >= session.max_reconnects:
            await self._give_up(session, generation)
            return

        session.is_reconnecting = True
        session.reconnect_count += 1
        attempt = session.reconnect_count

        # Exponential backoff: 0.5s, 1s, 2s, 4s, 5s max (fast recovery for voice calls)
        delay = min(0.5 * (2 ** (attempt - 1)), 5)
        print(f"{p} Reconnecting (attempt {attempt}/{session.max_reconnects}) in {delay}s...")
        await safe_send(session.portal_ws, {
            "type": "reconnecting",
            "data": {"attempt": attempt, "max": session.max_reconnects, "delay": delay}
        })

        await asyncio.sleep(delay)

        # Torn down or taken over during the backoff: a reconnect that went on
        # would resurrect a clientless session the reaper can never evict.
        if superseded(session, generation):
            print(f"{p} Session re-attached during backoff — abandoning reconnect")
            return
        if session.intentional_disconnect:
            print(f"{p} Session closed during backoff — abandoning reconnect")
            session.is_reconnecting = False
            return

        try:
            # The reply the old socket carried will never complete there.
            a.flush(session)
            # Stop the old listener FIRST: bound to the old socket, it would
            # observe the close below.
            listener = session.listener_task
            if listener is not None and not listener.done():
                listener.cancel()
            old = getattr(session, a.ws_attr, None)
            if old is not None:
                await close_upstream(session, a.ws_attr, old)

            # The adapter's dial installs the new socket only while this attach
            # is current (a re-attach during the dial keeps its own).
            if await a.redial(session):
                dialed = getattr(session, a.ws_attr, None)
                if self._torn_down(session, generation, dialed):
                    await self._drop_dialed(session, generation, dialed)
                    return

                if await a.reconfigure(session) and session.provenance:
                    # The rebuilt system context: keep the client's view in sync.
                    await safe_send(session.portal_ws, {
                        "type": "provenance",
                        "data": session.provenance
                    })

                # The point of no return: configure and provenance each awaited.
                if self._torn_down(session, generation, dialed):
                    await self._drop_dialed(session, generation, dialed)
                    return

                # A NEW listener: the old one's `async for` was bound to the
                # closed socket, and without one nothing reads the new
                # connection while the client is told "reconnected".
                if session.listener_task is not None and not session.listener_task.done():
                    session.listener_task.cancel()
                session.listener_task = asyncio.create_task(a.listener(session))
                # The keepalive ends when the socket it fed closes.
                if session.keepalive_task is None or session.keepalive_task.done():
                    session.keepalive_task = asyncio.create_task(a.keepalive(session))

                # reconnect_count is NOT reset here: the first real frame on the
                # new socket does that (a handshake that succeeds and drops at
                # once would otherwise make max_reconnects unreachable).
                session.is_reconnecting = False
                session.last_ai_message_time = time.time()
                session.status = "connected"
                print(f"{p} Reconnected successfully on attempt {attempt}")
                await safe_send(session.portal_ws, {
                    "type": "reconnected",
                    "data": {"attempt": attempt}
                })
            else:
                if superseded(session, generation):
                    print(f"{p} Session re-attached during the dial — abandoning reconnect")
                    return
                print(f"{p} Reconnect attempt {attempt} failed")
                session.is_reconnecting = False
                spawn_reconnect(session, a.reconnect(session))

        except Exception as e:
            print(f"{p} Reconnect error: {e}")
            if superseded(session, generation):
                return
            session.is_reconnecting = False
            spawn_reconnect(session, a.reconnect(session))

    @staticmethod
    def _torn_down(session, generation: int, dialed) -> bool:
        return session.intentional_disconnect or superseded(session, generation)

    async def _drop_dialed(self, session, generation: int, dialed) -> None:
        """Teardown ran (or a re-attach took over) while a reconnect dialed:
        close only the socket THIS reconnect opened."""
        if dialed is not None:
            await close_upstream(session, self.adapter.ws_attr, dialed)
        if not superseded(session, generation):
            session.is_reconnecting = False

    async def _give_up(self, session, generation: int) -> None:
        a = self.adapter
        print(f"{a.log_prefix} Max reconnects ({session.max_reconnects}) reached, giving up")
        session.status = "disconnected"
        await safe_send(session.portal_ws, {
            "type": "disconnected",
            "data": "Connection lost after multiple reconnection attempts"
        })
        dead = getattr(session, a.ws_attr, None)
        await a.save(session)
        if superseded(session, generation):
            return  # a re-attach during the save owns the session now
        # Null the closed-but-non-None socket, so the keepalive loop stops once
        # instead of failing on it and re-triggering this give-up (a second save).
        if dead is not None:
            await close_upstream(session, a.ws_attr, dead)
        if a.terminal_close:
            # A dead session must not sit there answering pings as if connected.
            await close_portal_ws(session, reason="max reconnects reached")

    # =========================================================================
    # Keepalive
    # =========================================================================

    async def keepalive_loop(self, session) -> None:
        """Send periodic keepalive silence so the provider's idle timeout never
        ends a quiet session.

        Liveness is the transport's job, not this loop's: the upstream runs
        websockets' ping/pong (ping_interval=20, ping_timeout=30), so a dead
        socket surfaces as ConnectionClosed in the listener within ~50 s, and a
        failed send here reconnects too. A silent provider is not a dead one:
        none of them sends anything while the user is quiet, and the old 60 s
        no-message timer forced reconnects on ordinary pauses (audit
        O09-live-voice-providers-02).
        """
        a = self.adapter
        p = a.log_prefix
        interval = a.keepalive_interval
        print(f"{p} Keepalive loop started (interval={interval}s)")

        try:
            while True:
                await asyncio.sleep(interval)

                if not getattr(session, a.ws_attr, None) or session.intentional_disconnect:
                    break

                try:
                    ws = getattr(session, a.ws_attr, None)
                    frame = a.keepalive_frame(session)
                    if ws and frame is not None:
                        await ws.send(json.dumps(frame))
                except websockets.exceptions.ConnectionClosed:
                    print(f"{p} Keepalive failed - connection closed")
                    if (not session.is_reconnecting and not session.intentional_disconnect
                            and not getattr(session, "external_reconnect", False)):
                        spawn_reconnect(session, a.reconnect(session))
                    break
                except Exception as e:
                    print(f"{p} Keepalive error: {e}")

        except asyncio.CancelledError:
            pass
        finally:
            print(f"{p} Keepalive loop stopped")

    # =========================================================================
    # Upstream listener
    # =========================================================================

    async def listener(self, session) -> None:
        """Read the provider's frames and hand each to the adapter."""
        a = self.adapter
        p = a.log_prefix
        try:
            async for message in getattr(session, a.ws_attr):
                event = None
                try:
                    event = json.loads(message)
                    a.on_frame(session)
                    await a.handle_event(session, event)
                except json.JSONDecodeError:
                    print(f"{p} Invalid JSON from {a.name}: {message[:100]}")
                except Exception as e:
                    print(f"{p} Error handling {a.name} message: {e}")
                    await a.answer_failed_event(session, event, e)

        except websockets.exceptions.ConnectionClosed as e:
            rcvd = getattr(e, "rcvd", None)
            close_code = getattr(rcvd, "code", None)
            close_reason = getattr(rcvd, "reason", "") or ""
            print(f"{p} {a.name} connection closed: code={close_code} reason={close_reason!r}")
            if not session.intentional_disconnect and not session.is_reconnecting:
                if a.forward_close_frames:
                    # Setup rejections (the 1007 invalid tool schema that killed
                    # every Gemini session for two weeks) arrive ONLY as close
                    # frames. `data` stays a string (the client contract);
                    # code/reason ride as additive fields.
                    await safe_send(session.portal_ws, {
                        "type": "error",
                        "data": f"{a.name} connection closed (code={close_code}): "
                                f"{close_reason or 'no reason given'}",
                        "code": close_code,
                        "reason": close_reason,
                    })
                if getattr(session, "external_reconnect", False):
                    # The session's owner (the phone bridge) re-dials when this
                    # listener returns; a second dialer here would race it.
                    print(f"{p} Unexpected disconnect - reconnect left to the session owner")
                else:
                    print(f"{p} Unexpected disconnect - triggering reconnect")
                    spawn_reconnect(session, a.reconnect(session))
            elif session.intentional_disconnect or not a.quiet_reconnecting_close:
                session.status = "disconnected"
                await safe_send(session.portal_ws, {
                    "type": "disconnected",
                    "data": f"{a.name} connection closed"
                })
            # else: the OLD socket closing mid-reconnect — the reconnect owns
            # the status and the client notices.

        except Exception as e:
            print(f"{p} {a.name} listener error: {e}")
            session.status = "error"
            if a.terminal_close:
                # Nothing re-triggers a reconnect from here: tell the client and
                # close instead of leaving a zombie WS answering pings.
                await safe_send(session.portal_ws, {
                    "type": "error",
                    "data": f"{a.name} listener error: {e}",
                })
                await close_portal_ws(session, reason=f"{a.name.lower()} listener error")
