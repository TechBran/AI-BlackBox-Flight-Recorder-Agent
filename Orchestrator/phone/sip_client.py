#!/usr/bin/env python3
"""
sip_client.py - SIP Integration via Drachtio and FreeSwitch

Provides SIP call handling using:
- Drachtio: SIP signaling (INVITE, BYE, etc.)
- FreeSwitch: Media handling via ESL (Event Socket Library)

Architecture:
    3CX Cloud PBX ↔ FreeSwitch (SIP/RTP) ↔ ESL ↔ Orchestrator
"""

import asyncio
import json
from typing import Optional, Callable, Awaitable, Dict, Any
from dataclasses import dataclass

from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    CallDirection,
    PHONE_SESSIONS,
)
from Orchestrator.volume import now_utc_iso

# ESL library import (optional - graceful degradation)
try:
    import greenswitch
    GREENSWITCH_AVAILABLE = True
except ImportError:
    GREENSWITCH_AVAILABLE = False
    print("[PHONE] greenswitch library not installed - run: pip install greenswitch")


@dataclass
class ESLConnection:
    """FreeSwitch ESL connection wrapper."""
    host: str
    port: int
    password: str
    conn: Optional[Any] = None
    connected: bool = False


class FreeSwitchClient:
    """
    FreeSwitch Event Socket Library (ESL) client.

    Handles media control for phone calls:
    - Answer/hangup calls
    - Play audio files or TTS
    - Detect DTMF input
    - Bridge audio streams
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8021,
        password: str = "ClueCon",
    ):
        self.host = host
        self.port = port
        self.password = password
        self._conn: Optional[Any] = None
        self._connected = False
        self._event_handlers: Dict[str, Callable] = {}

        # Callback for incoming calls
        self.on_incoming_call: Optional[Callable[[Dict], Awaitable[None]]] = None
        # Callback for DTMF events
        self.on_dtmf: Optional[Callable[[str, str], Awaitable[None]]] = None
        # Callback for hangup events
        self.on_hangup: Optional[Callable[[str], Awaitable[None]]] = None

    @property
    def is_connected(self) -> bool:
        """Check if connected to FreeSwitch."""
        return self._connected

    async def connect(self) -> bool:
        """
        Connect to FreeSwitch ESL.

        Returns:
            True if connection successful, False otherwise.
        """
        if not GREENSWITCH_AVAILABLE:
            print("[FREESWITCH] greenswitch library not available")
            return False

        try:
            print(f"[FREESWITCH] Connecting to {self.host}:{self.port}...")
            self._conn = greenswitch.InboundESL(
                host=self.host,
                port=self.port,
                password=self.password
            )
            await asyncio.get_event_loop().run_in_executor(None, self._conn.connect)
            self._connected = True
            print(f"[FREESWITCH] Connected successfully")

            # Subscribe to events
            await self._subscribe_events()

            return True

        except Exception as e:
            print(f"[FREESWITCH] Connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self):
        """Disconnect from FreeSwitch ESL."""
        if self._conn:
            try:
                self._conn.stop()
            except:
                pass
            self._conn = None
        self._connected = False
        print("[FREESWITCH] Disconnected")

    async def _subscribe_events(self):
        """Subscribe to FreeSwitch events."""
        if not self._conn:
            return

        # Subscribe to relevant events
        events = [
            "CHANNEL_CREATE",
            "CHANNEL_ANSWER",
            "CHANNEL_HANGUP",
            "CHANNEL_DESTROY",
            "DTMF",
            "PLAYBACK_START",
            "PLAYBACK_STOP",
            "RECORD_START",
            "RECORD_STOP",
        ]

        for event in events:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._conn.send(f"event plain {event}")
                )
            except Exception as e:
                print(f"[FREESWITCH] Failed to subscribe to {event}: {e}")

        print(f"[FREESWITCH] Subscribed to events: {', '.join(events)}")

    async def answer_call(self, uuid: str) -> bool:
        """
        Answer an incoming call.

        Args:
            uuid: FreeSwitch channel UUID

        Returns:
            True if successful
        """
        return await self._execute(uuid, "answer")

    async def hangup_call(self, uuid: str, cause: str = "NORMAL_CLEARING") -> bool:
        """
        Hang up a call.

        Args:
            uuid: FreeSwitch channel UUID
            cause: Hangup cause code

        Returns:
            True if successful
        """
        return await self._execute(uuid, f"hangup {cause}")

    async def play_audio(self, uuid: str, file_path: str) -> bool:
        """
        Play an audio file to the caller.

        Args:
            uuid: FreeSwitch channel UUID
            file_path: Path to audio file (WAV format)

        Returns:
            True if successful
        """
        return await self._execute(uuid, f"playback {file_path}")

    async def play_tts(self, uuid: str, text: str, voice: str = "default") -> bool:
        """
        Play TTS audio to the caller using FreeSwitch mod_tts.

        Args:
            uuid: FreeSwitch channel UUID
            text: Text to speak
            voice: TTS voice name

        Returns:
            True if successful
        """
        # Escape special characters
        escaped_text = text.replace("'", "\\'")
        return await self._execute(uuid, f"speak {voice}|{escaped_text}")

    async def start_dtmf_detection(self, uuid: str) -> bool:
        """
        Start DTMF detection on a channel.

        Args:
            uuid: FreeSwitch channel UUID

        Returns:
            True if successful
        """
        return await self._execute(uuid, "start_dtmf")

    async def stop_dtmf_detection(self, uuid: str) -> bool:
        """
        Stop DTMF detection on a channel.

        Args:
            uuid: FreeSwitch channel UUID

        Returns:
            True if successful
        """
        return await self._execute(uuid, "stop_dtmf")

    async def send_dtmf(self, uuid: str, digits: str, duration_ms: int = 100) -> bool:
        """
        Send DTMF digits on a channel (for outbound signaling).

        Args:
            uuid: FreeSwitch channel UUID
            digits: DTMF digits to send
            duration_ms: Digit duration in milliseconds

        Returns:
            True if successful
        """
        return await self._execute(uuid, f"send_dtmf {digits}@{duration_ms}")

    async def bridge_to_ws(self, uuid: str, ws_url: str) -> bool:
        """
        Bridge the call audio to a WebSocket endpoint.

        Uses FreeSwitch mod_audio_stream to send/receive audio via WebSocket.

        Args:
            uuid: FreeSwitch channel UUID
            ws_url: WebSocket URL for audio streaming

        Returns:
            True if successful
        """
        # Start audio stream to WebSocket
        return await self._execute(
            uuid,
            f"uuid_audio_stream {uuid} start {ws_url} both 16k"
        )

    async def stop_bridge(self, uuid: str) -> bool:
        """
        Stop the WebSocket audio bridge.

        Args:
            uuid: FreeSwitch channel UUID

        Returns:
            True if successful
        """
        return await self._execute(uuid, f"uuid_audio_stream {uuid} stop")

    async def originate_call(
        self,
        destination: str,
        caller_id: str,
        gateway: str = "3cx",
        timeout_s: int = 30,
    ) -> Optional[str]:
        """
        Originate an outbound call.

        Args:
            destination: Destination phone number (E.164)
            caller_id: Caller ID to display
            gateway: SIP gateway name
            timeout_s: Ring timeout in seconds

        Returns:
            Channel UUID if successful, None otherwise
        """
        if not self._conn or not self._connected:
            print("[FREESWITCH] Not connected")
            return None

        try:
            # Originate command
            originate_str = (
                f"originate {{origination_caller_id_number={caller_id},"
                f"originate_timeout={timeout_s},"
                f"ignore_early_media=true}}"
                f"sofia/gateway/{gateway}/{destination} "
                f"&park()"
            )

            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._conn.send(f"bgapi {originate_str}")
            )

            # Parse UUID from response
            if response and hasattr(response, 'data'):
                data = response.data
                if "OK" in data:
                    # Extract UUID
                    parts = data.split()
                    if len(parts) >= 2:
                        return parts[1]

            return None

        except Exception as e:
            print(f"[FREESWITCH] Originate failed: {e}")
            return None

    async def _execute(self, uuid: str, command: str) -> bool:
        """
        Execute a FreeSwitch command on a channel.

        Args:
            uuid: FreeSwitch channel UUID
            command: Command to execute

        Returns:
            True if successful
        """
        if not self._conn or not self._connected:
            print(f"[FREESWITCH] Not connected, cannot execute: {command}")
            return False

        try:
            full_command = f"uuid_{command}" if not command.startswith("uuid_") else command
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._conn.send(f"api {full_command}")
            )
            print(f"[FREESWITCH] Executed: {command} -> {response}")
            return True

        except Exception as e:
            print(f"[FREESWITCH] Execute failed for '{command}': {e}")
            return False

    async def listen_events(self):
        """
        Listen for FreeSwitch events in background.

        This should be run as an asyncio task.
        """
        if not self._conn:
            return

        print("[FREESWITCH] Starting event listener...")

        while self._connected:
            try:
                # Get next event (blocking with timeout)
                event = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._conn.receive_event()
                )

                if event:
                    await self._handle_event(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[FREESWITCH] Event listener error: {e}")
                await asyncio.sleep(0.1)

        print("[FREESWITCH] Event listener stopped")

    async def _handle_event(self, event: Any):
        """Handle a FreeSwitch event."""
        event_name = event.get("Event-Name", "")
        uuid = event.get("Unique-ID", "")

        if event_name == "CHANNEL_CREATE":
            # New incoming call
            caller = event.get("Caller-Caller-ID-Number", "")
            callee = event.get("Caller-Destination-Number", "")
            print(f"[FREESWITCH] Incoming call: {caller} -> {callee} (UUID: {uuid})")

            if self.on_incoming_call:
                await self.on_incoming_call({
                    "uuid": uuid,
                    "caller": caller,
                    "callee": callee,
                    "event": event,
                })

        elif event_name == "DTMF":
            # DTMF digit detected
            digit = event.get("DTMF-Digit", "")
            # Never log the digit itself (it may be part of a PIN).
            print(f"[FREESWITCH] DTMF received (UUID: {uuid})")

            if self.on_dtmf:
                await self.on_dtmf(uuid, digit)

        elif event_name == "CHANNEL_HANGUP":
            # Call ended
            cause = event.get("Hangup-Cause", "NORMAL_CLEARING")
            print(f"[FREESWITCH] Hangup: {cause} (UUID: {uuid})")

            if self.on_hangup:
                await self.on_hangup(uuid)


# Global FreeSwitch client instance
_freeswitch_client: Optional[FreeSwitchClient] = None


def get_freeswitch_client() -> Optional[FreeSwitchClient]:
    """Get the global FreeSwitch client instance."""
    return _freeswitch_client


async def init_freeswitch(host: str, port: int, password: str) -> FreeSwitchClient:
    """
    Initialize the global FreeSwitch client.

    Args:
        host: FreeSwitch ESL host
        port: FreeSwitch ESL port
        password: FreeSwitch ESL password

    Returns:
        FreeSwitchClient instance
    """
    global _freeswitch_client

    _freeswitch_client = FreeSwitchClient(host, port, password)
    await _freeswitch_client.connect()

    return _freeswitch_client
