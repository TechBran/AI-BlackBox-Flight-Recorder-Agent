#!/usr/bin/env python3
"""
client.py - Asterisk REST Interface (ARI) Client

Provides:
1. HTTP methods for channel control (answer, hangup, originate, DTMF, playback)
2. WebSocket connection for ARI events (StasisStart, DTMF, Hangup)

Events are routed per call. The TG200 carries one call per GSM port, so two
calls at once is the designed case: each call registers its DTMF handler and
its hangup watch by channel id, and an outbound call waits for its answer by
the uuid it put in appArgs. They used to swap shared callback slots and
restore them in `finally`, which orphaned the second of two concurrent calls
(audit O11-telephony-asterisk-cellular-04); hangup events reached a slot
nothing set (-11).

Uses aiohttp (already in venv) for both HTTP and WebSocket.
Singleton pattern: get_ari_client() / init_ari_client().
"""

import asyncio
import collections
import json
import time
import traceback
from typing import Optional, Callable, Awaitable, Dict, Any

import aiohttp

from Orchestrator.lifespan import track_task
from Orchestrator.asterisk.config import (
    ASTERISK_ARI_URL,
    ASTERISK_ARI_WS_URL,
    ASTERISK_ARI_USER,
    ASTERISK_ARI_PASSWORD,
    ASTERISK_ARI_APP,
)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_ari_client: Optional["AsteriskARIClient"] = None


def get_ari_client() -> Optional["AsteriskARIClient"]:
    """Get the singleton ARI client (may be None if not initialized)."""
    return _ari_client


async def init_ari_client() -> Optional["AsteriskARIClient"]:
    """Initialize and connect the singleton ARI client."""
    global _ari_client
    if _ari_client and _ari_client.is_connected:
        return _ari_client

    client = AsteriskARIClient(
        ari_url=ASTERISK_ARI_URL,
        ari_ws_url=ASTERISK_ARI_WS_URL,
        username=ASTERISK_ARI_USER,
        password=ASTERISK_ARI_PASSWORD,
        app_name=ASTERISK_ARI_APP,
    )
    connected = await client.connect()
    if connected:
        _ari_client = client
        print(f"[ARI] Connected to Asterisk ARI at {ASTERISK_ARI_URL}")
        return client
    else:
        print(f"[ARI] Failed to connect to Asterisk ARI at {ASTERISK_ARI_URL}")
        return None


# ---------------------------------------------------------------------------
# ARI Client
# ---------------------------------------------------------------------------
class AsteriskARIClient:
    """
    Asterisk REST Interface client.

    Two connections:
    1. ARI Events WebSocket — receives StasisStart, DTMF, Hangup, etc.
    2. HTTP REST calls — channel control, originate, playback, etc.
    """

    def __init__(
        self,
        ari_url: str,
        ari_ws_url: str,
        username: str,
        password: str,
        app_name: str,
    ):
        self.ari_url = ari_url.rstrip("/")
        self.ari_ws_url = ari_ws_url
        self.auth = aiohttp.BasicAuth(username, password)
        self.app_name = app_name

        self._session: Optional[aiohttp.ClientSession] = None
        self._event_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._event_task: Optional[asyncio.Task] = None
        self._running = False
        self._reconnect_delay = 1.0

        # The default StasisStart handler: inbound calls (set by startup.py).
        # Outbound answers never reach it (expect_outbound_answer).
        self.on_stasis_start: Optional[Callable[..., Awaitable[None]]] = None

        # Per-call routing (see the module docstring)
        self._dtmf_handlers: Dict[str, Callable[[str, str], Awaitable[None]]] = {}
        self._outbound_waiters: Dict[str, asyncio.Future] = {}
        self._hangup_watches: Dict[str, asyncio.Event] = {}
        # Channels that hung up with nobody watching yet (bounded), so a watch
        # started a moment late still sees it.
        self._recently_gone: "collections.OrderedDict[str, None]" = collections.OrderedDict()

        # Playback completion tracking — maps playback_id to asyncio.Event
        self._playback_events: Dict[str, asyncio.Event] = {}
        # Playbacks that finished before anyone waited for them (bounded)
        self._recently_finished: "collections.OrderedDict[str, None]" = collections.OrderedDict()

    @property
    def is_connected(self) -> bool:
        return self._running and self._session is not None and not self._session.closed

    # ------------------------------------------------------------------
    # Per-call routing
    # ------------------------------------------------------------------

    _RECENT_MAX = 256

    def register_dtmf_handler(self, channel_id: str,
                              handler: Callable[[str, str], Awaitable[None]]) -> None:
        """Deliver this channel's DTMF digits to ``handler(channel_id, digit)``."""
        self._dtmf_handlers[channel_id] = handler

    def unregister_dtmf_handler(self, channel_id: str) -> None:
        self._dtmf_handlers.pop(channel_id, None)

    def expect_outbound_answer(self, call_uuid: str) -> asyncio.Future:
        """A future resolved with the channel id when the call originated with
        appArgs "outbound,<call_uuid>" is answered (enters Stasis)."""
        future = asyncio.get_running_loop().create_future()
        self._outbound_waiters[call_uuid] = future
        return future

    def cancel_outbound_answer(self, call_uuid: str) -> None:
        future = self._outbound_waiters.pop(call_uuid, None)
        if future and not future.done():
            future.cancel()

    def watch_hangup(self, channel_id: str) -> asyncio.Event:
        """An Event set once the channel hangs up (ChannelHangupRequest or
        ChannelDestroyed); already set when it is gone."""
        event = self._hangup_watches.get(channel_id)
        if event is None:
            event = asyncio.Event()
            if channel_id in self._recently_gone:
                event.set()
            self._hangup_watches[channel_id] = event
        return event

    def unwatch_hangup(self, channel_id: str) -> None:
        self._hangup_watches.pop(channel_id, None)

    def _remember(self, recent: "collections.OrderedDict", key: str) -> None:
        recent[key] = None
        recent.move_to_end(key)
        while len(recent) > self._RECENT_MAX:
            recent.popitem(last=False)

    def _channel_gone(self, channel_id: str) -> None:
        event = self._hangup_watches.get(channel_id)
        if event is not None:
            event.set()
        else:
            self._remember(self._recently_gone, channel_id)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to ARI: create HTTP session and start event WebSocket."""
        try:
            self._session = aiohttp.ClientSession(auth=self.auth)

            # Test HTTP connectivity
            info = await self._ari_get("/ari/asterisk/info")
            if info is None:
                await self._session.close()
                self._session = None
                return False

            version = info.get("system", {}).get("version", "unknown")
            print(f"[ARI] Asterisk version: {version}")

            # Start event WebSocket listener
            self._running = True
            self._event_task = asyncio.create_task(self._event_loop())
            return True

        except Exception as e:
            print(f"[ARI] Connect error: {e}")
            if self._session:
                await self._session.close()
                self._session = None
            return False

    async def disconnect(self):
        """Disconnect from ARI."""
        self._running = False
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        if self._event_ws and not self._event_ws.closed:
            await self._event_ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        print("[ARI] Disconnected")

    # ------------------------------------------------------------------
    # Event WebSocket
    # ------------------------------------------------------------------

    async def _event_loop(self):
        """Listen for ARI events on the WebSocket with auto-reconnect."""
        while self._running:
            try:
                ws_url = self.ari_ws_url
                print(f"[ARI] Connecting to event WebSocket: {ws_url}")
                self._event_ws = await self._session.ws_connect(ws_url)
                self._reconnect_delay = 1.0  # Reset on success
                print("[ARI] Event WebSocket connected")

                async for msg in self._event_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                            await self._handle_event(event)
                        except json.JSONDecodeError:
                            print(f"[ARI] Invalid JSON event: {msg.data[:200]}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[ARI] Event WebSocket error: {e}")

            if self._running:
                print(f"[ARI] Event WebSocket disconnected, reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _handle_event(self, event: dict):
        """Dispatch an ARI event to the appropriate callback."""
        event_type = event.get("type", "")

        if event_type == "StasisStart":
            channel = event.get("channel", {})
            channel_id = channel.get("id", "")
            caller = channel.get("caller", {}).get("number", "unknown")
            callee = channel.get("dialplan", {}).get("exten", "unknown")
            args = event.get("args", [])
            print(f"[ARI] StasisStart: channel={channel_id} caller={caller} callee={callee} args={args}")
            if args and args[0] == "outbound":
                # An answered outbound call: hand it to the call that dialled it.
                waiter = self._outbound_waiters.pop(args[1] if len(args) > 1 else "", None)
                if waiter and not waiter.done():
                    waiter.set_result(channel_id)
                else:
                    # Its call gave up (no-answer timeout). Never run the
                    # inbound IVR on the person being called.
                    print(f"[ARI] Outbound answer nobody is waiting for, hanging up: channel={channel_id}")
                    self._spawn(self.hangup_channel(channel_id), "ari-stray-outbound")
            elif self.on_stasis_start:
                self._spawn(self.on_stasis_start(channel_id, caller, callee, args), "ari-stasis-start")

        elif event_type == "StasisEnd":
            # Also sent when a channel is continued into the dialplan, so it
            # is not a hangup.
            channel_id = event.get("channel", {}).get("id", "")
            print(f"[ARI] StasisEnd: channel={channel_id}")

        elif event_type == "ChannelDtmfReceived":
            channel_id = event.get("channel", {}).get("id", "")
            digit = event.get("digit", "")
            # Never log the digit: IVR PIN entry arrives through here.
            print(f"[ARI] DTMF received: channel={channel_id}")
            handler = self._dtmf_handlers.get(channel_id)
            if handler:
                self._spawn(handler(channel_id, digit), "ari-dtmf")

        elif event_type in ("ChannelHangupRequest", "ChannelDestroyed"):
            channel_id = event.get("channel", {}).get("id", "")
            label = "HangupRequest" if event_type == "ChannelHangupRequest" else "ChannelDestroyed"
            print(f"[ARI] {label}: channel={channel_id}")
            if channel_id:
                self._channel_gone(channel_id)

        elif event_type == "PlaybackFinished":
            playback_id = event.get("playback", {}).get("id", "")
            if playback_id:
                # Signal any waiter for this playback
                evt = self._playback_events.pop(playback_id, None)
                if evt:
                    evt.set()
                else:
                    self._remember(self._recently_finished, playback_id)

    @staticmethod
    def _spawn(coro, name: str) -> None:
        """Run an event callback as its own task, held by the app's lifespan
        registry until it ends (the StasisStart handler runs the whole call)
        and cancelled at shutdown before this client disconnects."""
        track_task(asyncio.create_task(coro, name=name), name)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _ari_get(self, path: str) -> Optional[dict]:
        try:
            url = f"{self.ari_url}{path}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    print(f"[ARI] GET {path} → {resp.status}: {text[:200]}")
                    return None
        except Exception as e:
            print(f"[ARI] GET {path} error: {e}")
            return None

    async def _ari_post(self, path: str, json_data: dict = None, params: dict = None) -> Optional[dict]:
        try:
            url = f"{self.ari_url}{path}"
            async with self._session.post(url, json=json_data, params=params) as resp:
                if resp.status in (200, 204):
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return {"status": "ok"}
                else:
                    text = await resp.text()
                    print(f"[ARI] POST {path} → {resp.status}: {text[:200]}")
                    return None
        except Exception as e:
            print(f"[ARI] POST {path} error: {e}")
            return None

    async def _ari_delete(self, path: str, params: dict = None) -> bool:
        try:
            url = f"{self.ari_url}{path}"
            async with self._session.delete(url, params=params) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print(f"[ARI] DELETE {path} error: {e}")
            return False

    # ------------------------------------------------------------------
    # Channel control
    # ------------------------------------------------------------------

    async def answer_channel(self, channel_id: str) -> bool:
        """Answer an incoming channel."""
        print(f"[ARI] Answering channel: {channel_id}")
        result = await self._ari_post(f"/ari/channels/{channel_id}/answer")
        print(f"[ARI] Answer result: {result}")
        return result is not None

    async def continue_in_dialplan(self, channel_id: str, context: str = "blackbox-audiosocket",
                                    extension: str = "s", priority: int = 1) -> bool:
        """Move a Stasis channel back into the Asterisk dialplan.
        Used after ARI-originated outbound calls are answered — sends channel
        to blackbox-outbound context where AudioSocket() bridges the audio."""
        result = await self._ari_post(
            f"/ari/channels/{channel_id}/continue",
            params={"context": context, "extension": extension, "priority": priority}
        )
        return result is not None

    async def hangup_channel(self, channel_id: str, reason: str = "normal") -> bool:
        """Hang up a channel."""
        return await self._ari_delete(
            f"/ari/channels/{channel_id}",
            params={"reason_code": reason}
        )

    async def get_channel_info(self, channel_id: str) -> Optional[dict]:
        """Get channel details."""
        return await self._ari_get(f"/ari/channels/{channel_id}")

    async def set_variable(self, channel_id: str, variable: str, value: str) -> bool:
        """Set a channel variable via ARI."""
        result = await self._ari_post(
            f"/ari/channels/{channel_id}/variable",
            params={"variable": variable, "value": value}
        )
        return result is not None

    async def list_channels(self) -> list:
        """List all active channels."""
        result = await self._ari_get("/ari/channels")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Originate (outbound calls)
    # ------------------------------------------------------------------

    async def originate(
        self,
        endpoint: str,
        callerid: str = "",
        timeout: int = 45,
        variables: dict = None,
        app_args: str = "",
    ) -> Optional[str]:
        """
        Originate an outbound call via ARI Stasis.

        The channel enters Stasis when the callee answers.
        Use app_args to pass data to the StasisStart handler
        (e.g., "outbound,{uuid}" to identify the call).
        """
        data = {
            "endpoint": endpoint,
            "timeout": timeout,
            "app": self.app_name,
        }
        if app_args:
            data["appArgs"] = app_args
        if callerid:
            data["callerId"] = callerid
        if variables:
            data["variables"] = {"type": "ChannelDialplanVariable", "variables": variables}

        result = await self._ari_post("/ari/channels", json_data=data)
        if result and "id" in result:
            channel_id = result["id"]
            print(f"[ARI] Originated call: {endpoint} → channel={channel_id}")
            return channel_id
        return None

    # ------------------------------------------------------------------
    # DTMF
    # ------------------------------------------------------------------

    async def send_dtmf(self, channel_id: str, dtmf: str, duration: int = 100) -> bool:
        """Send DTMF tones to a channel."""
        result = await self._ari_post(
            f"/ari/channels/{channel_id}/dtmf",
            params={"dtmf": dtmf, "duration": duration}
        )
        return result is not None

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    async def play_sound(self, channel_id: str, media: str) -> Optional[str]:
        """
        Play a sound file on a channel.

        Args:
            media: Sound URI, e.g. "sound:hello-world" or "recording:my-recording"

        Returns:
            Playback ID if successful.
        """
        result = await self._ari_post(
            f"/ari/channels/{channel_id}/play",
            params={"media": media}
        )
        if result and "id" in result:
            return result["id"]
        return None

    async def stop_playback(self, playback_id: str) -> bool:
        """Stop a playback in progress."""
        # Clean up any pending wait event
        self._playback_events.pop(playback_id, None)
        return await self._ari_delete(f"/ari/playbacks/{playback_id}")

    async def wait_for_playback(self, playback_id: str, timeout: float = 15.0) -> bool:
        """Wait for a playback to finish via PlaybackFinished event.

        Returns True if playback finished, False on timeout.
        """
        if playback_id in self._recently_finished:
            del self._recently_finished[playback_id]
            return True  # it finished before this wait began
        evt = asyncio.Event()
        self._playback_events[playback_id] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self._playback_events.pop(playback_id, None)
            return False

    # ------------------------------------------------------------------
    # Asterisk system info
    # ------------------------------------------------------------------

    async def get_asterisk_info(self) -> Optional[dict]:
        """Get Asterisk system info (version, uptime, etc.)."""
        return await self._ari_get("/ari/asterisk/info")

    async def get_endpoints(self) -> list:
        """List all PJSIP endpoints."""
        result = await self._ari_get("/ari/endpoints")
        return result if isinstance(result, list) else []

    async def get_endpoint_detail(self, tech: str, resource: str) -> Optional[dict]:
        """Get details for a specific endpoint."""
        return await self._ari_get(f"/ari/endpoints/{tech}/{resource}")
