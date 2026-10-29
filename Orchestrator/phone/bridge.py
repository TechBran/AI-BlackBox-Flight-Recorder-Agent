#!/usr/bin/env python3
"""
bridge.py - Phone to AI WebSocket Bridge

Bridges phone audio to AI backends. The live-voice backends (OpenAI Realtime,
Gemini Live, Grok) run on the routes' shared LiveVoiceBridge
(Orchestrator/voice/bridge.py): the same listener, tool ladder, prompt and
transcript save as the Portal, so the phone works exactly like the Portal UI.
This module adds the call's audio conversion and its own reconnect loop. The
Claude Code backend is an STT -> CLI -> TTS pipeline of its own.

Audio Flow:
    Phone (8kHz ULAW / PCM16) → Convert → provider input frame → AI Backend
    AI Backend → LiveVoiceBridge → PhoneWebSocketAdapter → Convert → Phone
"""

import asyncio
import json
import base64
import traceback
from typing import Optional, Any, Callable, Awaitable

from starlette.websockets import WebSocketState

from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    AIBackend,
)
from Orchestrator.phone.audio_converter import AudioConverter
from Orchestrator.utils.paths import blackbox_root
from Orchestrator.volume import now_utc_iso

# The live-voice session registries a call leg is entered in
from Orchestrator.models import (
    REALTIME_SESSIONS,
    GEMINI_LIVE_SESSIONS,
    GROK_LIVE_SESSIONS,
)

# WebSocket library
try:
    import websockets
    import websockets.exceptions
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[PHONE-BRIDGE] websockets library not installed")

# HTTP client
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("[PHONE-BRIDGE] aiohttp library not installed")

# WebRTC VAD for robust voice activity detection (the Claude Code backend)
try:
    import webrtcvad
    WEBRTCVAD_AVAILABLE = True
except ImportError:
    WEBRTCVAD_AVAILABLE = False
    print("[PHONE-BRIDGE] webrtcvad not installed - using energy-based VAD")


class PhoneWebSocketAdapter:
    """
    Adapter that makes a phone call look like a Portal WebSocket to the
    live-voice bridge.

    The bridge's handlers send client frames through send_json(); here they
    are routed to the call:
    - audio_delta: 24kHz PCM16 → the call (PCM16 for cellular, µ-law for Twilio)
    - response_complete: the reply's transcript → on_ai_transcript
    - other events: logged

    ``application_state`` reports CONNECTED while the call runs: the bridge's
    send helper only delivers to a connected client, and without it every frame
    was dropped (the caller never heard the model).
    """

    def __init__(self, bridge: 'PhoneAIBridge'):
        self.bridge = bridge
        self._is_speaking = False
        self._transcript_buffer = ""

    @property
    def application_state(self):
        return WebSocketState.CONNECTED if self.bridge.is_running else WebSocketState.DISCONNECTED

    async def send_json(self, data: dict):
        """
        Handle messages that would normally go to Portal WebSocket.
        Routes audio to phone, logs other events.
        """
        msg_type = data.get("type", "")

        if msg_type == "audio_delta":
            audio_b64 = data.get("data", "")
            if audio_b64 and (self.bridge.on_ai_pcm16 or self.bridge.on_ai_audio):
                try:
                    pcm_data = base64.b64decode(audio_b64)
                    if self.bridge.on_ai_pcm16:
                        # Direct PCM16 path (cellular — no ULAW)
                        await self.bridge.on_ai_pcm16(pcm_data, 24000)
                    else:
                        # Legacy ULAW path (Twilio)
                        ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)
                        await self.bridge.on_ai_audio(ulaw_data)
                    self._is_speaking = True
                except Exception as e:
                    print(f"[PHONE-ADAPTER] Error converting/sending audio: {e}")
                    if "closed" in str(e).lower():
                        self.bridge._running = False

        elif msg_type == "transcript_delta":
            # Accumulate transcript for logging
            text = data.get("data", "")
            self._transcript_buffer += text

        elif msg_type == "response_complete":
            # AI finished speaking - send transcript callback
            self._is_speaking = False

            response_data = data.get("data", {})
            transcript = response_data.get("transcript", "") or self._transcript_buffer

            if transcript and self.bridge.on_ai_transcript:
                try:
                    await self.bridge.on_ai_transcript(transcript)
                    print(f"[PHONE-ADAPTER] AI response complete: {transcript[:100]}...")
                except Exception as e:
                    print(f"[PHONE-ADAPTER] Error sending transcript: {e}")

            self._transcript_buffer = ""

        elif msg_type == "user_transcript":
            transcript = data.get("data", "")
            if transcript:
                print(f"[PHONE-ADAPTER] User said: {transcript[:100]}...")

        elif msg_type == "setup_complete":
            print(f"[PHONE-ADAPTER] Setup complete")

        elif msg_type == "tool_call":
            tool_data = data.get("data", {})
            print(f"[PHONE-ADAPTER] Tool call: {tool_data.get('name', 'unknown')}")

        elif msg_type == "tool_result":
            tool_data = data.get("data", {})
            print(f"[PHONE-ADAPTER] Tool result: {tool_data.get('name', 'unknown')}")

        elif msg_type == "error":
            error_msg = data.get("data", "Unknown error")
            print(f"[PHONE-ADAPTER] Error from the AI backend: {error_msg}")

        elif msg_type == "disconnected":
            print(f"[PHONE-ADAPTER] Disconnected: {data.get('data', '')}")

        else:
            # Log unknown message types for debugging
            print(f"[PHONE-ADAPTER] Unhandled message type: {msg_type}")

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking


class PhoneAIBridge:
    """
    Bridges a phone call to an AI backend.

    For the live-voice backends this class:
    1. Creates the provider's session object (RealtimeSession, GeminiLiveSession,
       GrokLiveSession) and enters it in the live-session registry
    2. Dials and configures it through the provider's LiveVoiceBridge adapter
       (with the provider's phone tuning: voice, server VAD)
    3. Runs the bridge's listener through a PhoneWebSocketAdapter, re-dialing
       when the provider drops (the bridge leaves the reconnect to it)
    4. Converts audio between the phone (8kHz ULAW or PCM16) and the provider's
       input rate, and plays the model's 24kHz audio on the call
    """

    def __init__(self, phone_session: PhoneSession):
        self.phone_session = phone_session
        self._running = False
        self._tasks: list = []

        # The actual AI session (RealtimeSession, GeminiLiveSession, or GrokLiveSession)
        self._ai_session: Optional[Any] = None
        # The provider's LiveVoiceBridge (live-voice backends only)
        self._leg = None
        self._reconnecting = False
        self._closed_send_logged = False

        # Audio conversion
        self.converter = AudioConverter()

        # Callbacks for sending audio/transcripts to phone
        self.on_ai_audio: Optional[Callable[[bytes], Awaitable[None]]] = None  # ULAW (Twilio)
        self.on_ai_pcm16: Optional[Callable[[bytes, int], Awaitable[None]]] = None  # PCM16 (cellular)
        self.on_ai_transcript: Optional[Callable[[str], Awaitable[None]]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, operator: str = "") -> bool:
        """Start the AI bridge."""
        if not WEBSOCKETS_AVAILABLE:
            print("[PHONE-BRIDGE] Cannot start - websockets not available")
            return False

        self.phone_session.operator = operator or self.phone_session.operator or "phone-caller"
        self._running = True

        try:
            backend = self.phone_session.ai_backend
            print(f"[PHONE-BRIDGE] Starting bridge for {backend.value} with operator {self.phone_session.operator}")

            if backend in (AIBackend.OPENAI_REALTIME, AIBackend.GEMINI_LIVE, AIBackend.GROK_LIVE):
                success = await self._start_live()
            elif backend == AIBackend.CLAUDE_CODE:
                success = await self._start_claude()
            else:
                print(f"[PHONE-BRIDGE] Unknown backend: {backend}")
                success = False

            if success:
                self.phone_session.status = PhoneStatus.BRIDGED
                self.phone_session.call_start = now_utc_iso()
                print(f"[PHONE-BRIDGE] Bridge started successfully")

                # Play ready tone to indicate AI is connected
                await self._play_ready_tone()

                # Start keepalive task to prevent connection dropout
                keepalive_task = asyncio.create_task(self._keepalive_loop())
                self._tasks.append(keepalive_task)
            else:
                self._running = False

            return success

        except Exception as e:
            print(f"[PHONE-BRIDGE] Start failed: {e}")
            traceback.print_exc()
            self._running = False
            return False

    async def _play_ready_tone(self):
        """Play a ready tone to indicate the AI is connected."""
        if self.on_ai_pcm16 or self.on_ai_audio:
            try:
                ready_tone = AudioConverter.generate_ready_tone()
                if self.on_ai_pcm16:
                    # Convert ULAW tone to PCM16 for cellular
                    pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ready_tone)
                    await self.on_ai_pcm16(pcm_8k, 8000)
                else:
                    await self.on_ai_audio(ready_tone)
                print("[PHONE-BRIDGE] Ready tone played")
            except Exception as e:
                print(f"[PHONE-BRIDGE] Failed to play ready tone: {e}")

    async def _play_listen_start_chime(self):
        """Play ascending chime when VAD starts listening (like ready tone style)."""
        if not self.on_ai_audio:
            return
        try:
            # Ascending C5 → E5 → G5 (musical triad)
            tone1 = AudioConverter.generate_tone(frequency=523, duration_ms=80, volume=0.3)   # C5
            silence = AudioConverter.generate_tone(frequency=0, duration_ms=30, volume=0.0)
            tone2 = AudioConverter.generate_tone(frequency=659, duration_ms=80, volume=0.3)   # E5
            tone3 = AudioConverter.generate_tone(frequency=784, duration_ms=100, volume=0.3)  # G5
            await self.on_ai_audio(tone1 + silence + tone2 + silence + tone3)
            print("[PHONE-BRIDGE] Played listen start chime")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to play listen start chime: {e}")

    async def _play_listen_stop_chime(self):
        """Play descending chime when VAD stops listening (speech ended)."""
        if not self.on_ai_audio:
            return
        try:
            # Descending G5 → E5 → C5 (reverse triad)
            tone1 = AudioConverter.generate_tone(frequency=784, duration_ms=80, volume=0.3)   # G5
            silence = AudioConverter.generate_tone(frequency=0, duration_ms=30, volume=0.0)
            tone2 = AudioConverter.generate_tone(frequency=659, duration_ms=80, volume=0.3)   # E5
            tone3 = AudioConverter.generate_tone(frequency=523, duration_ms=100, volume=0.3)  # C5
            await self.on_ai_audio(tone1 + silence + tone2 + silence + tone3)
            print("[PHONE-BRIDGE] Played listen stop chime")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to play listen stop chime: {e}")

    def _generate_hold_music(self) -> bytes:
        """Load pre-converted Lyria ambient music for hold.

        Uses smooth ambient electronic music generated by Lyria, pre-converted
        to 8kHz ULAW for phone playback. Falls back to synthetic tones if file
        not found.

        The Lyria music file is ~32 seconds of smooth ambient loops.
        """
        import os

        # Path to pre-converted Lyria hold music
        audio_dir = os.path.dirname(os.path.abspath(__file__))
        lyria_path = os.path.join(audio_dir, "audio", "hold_music_lyria.raw")

        if os.path.exists(lyria_path):
            try:
                with open(lyria_path, 'rb') as f:
                    hold_music = f.read()
                print(f"[PHONE-BRIDGE] Loaded Lyria hold music: {len(hold_music)} bytes ({len(hold_music)/8000:.1f}s)")
                return hold_music
            except Exception as e:
                print(f"[PHONE-BRIDGE] Failed to load Lyria hold music: {e}")

        # Fallback to synthetic tones if Lyria file not available
        print("[PHONE-BRIDGE] Lyria hold music not found, using synthetic fallback")
        return self._generate_synthetic_hold_music()

    def _generate_synthetic_hold_music(self) -> bytes:
        """Generate synthetic hold music as fallback.

        Creates a gentle chord progression with harmonies.
        Pattern: C major → A minor → F major → G major
        """
        import struct
        import math

        sample_rate = 24000
        base_volume = 0.12

        chords = [
            [262, 330, 392],  # C major
            [220, 262, 330],  # A minor
            [175, 220, 262],  # F major
            [196, 247, 294],  # G major
        ]

        chord_duration_ms = 500
        samples_per_chord = int(sample_rate * chord_duration_ms / 1000)

        pcm_samples = []

        for chord_freqs in chords:
            for i in range(samples_per_chord):
                t = i / sample_rate
                fade_samples = samples_per_chord // 6
                if i < fade_samples:
                    envelope = i / fade_samples
                elif i > samples_per_chord - fade_samples:
                    envelope = (samples_per_chord - i) / fade_samples
                else:
                    envelope = 1.0

                sample_value = 0.0
                for freq in chord_freqs:
                    sample_value += math.sin(2 * math.pi * freq * t)

                sample_value = sample_value / 3.0
                sample = int(32767 * base_volume * envelope * sample_value)
                pcm_samples.append(max(-32768, min(32767, sample)))

        pcm_data = struct.pack(f"<{len(pcm_samples)}h", *pcm_samples)
        return AudioConverter.ai_to_phone(pcm_data, 24000)

    async def _keepalive_loop(self):
        """
        Send periodic keepalive silence so a quiet call never hits the
        provider's idle timeout.

        A caller who listens, thinks or is on hold is NOT a dead connection:
        the providers send nothing while the line is quiet, and the old 60 s
        no-message timer reconnected them, rebuilding the session WITHOUT the
        call's conversation (audit O10-telephony-twilio-08). Liveness is the
        socket's own ping/pong: a dead upstream surfaces as ConnectionClosed
        in the listener, whose loop (_listener_with_reconnect) re-dials. A
        keepalive that keeps failing otherwise closes the upstream so that loop
        re-dials it.
        """
        keepalive_interval = 10  # seconds
        consecutive_failures = 0
        max_consecutive_failures = 3

        print(f"[PHONE-BRIDGE] Health monitor started (keepalive={keepalive_interval}s)")

        try:
            while self._running:
                await asyncio.sleep(keepalive_interval)

                if not self._running:
                    break
                if self._leg is None:
                    continue  # Claude is text-based: no provider socket to keep alive

                try:
                    await self._send_keepalive()
                    if consecutive_failures > 0:
                        print(f"[PHONE-BRIDGE] Keepalive recovered after {consecutive_failures} failures")
                    consecutive_failures = 0

                except websockets.exceptions.ConnectionClosed as e:
                    # The listener sees the same close and its loop re-dials.
                    print(f"[PHONE-BRIDGE] Keepalive: connection closed ({getattr(e.rcvd, 'code', None)}); "
                          f"the listener reconnects")

                except Exception as e:
                    consecutive_failures += 1
                    print(f"[PHONE-BRIDGE] ⚠️ Keepalive error ({consecutive_failures}/{max_consecutive_failures}): {e}")

                    if consecutive_failures >= max_consecutive_failures:
                        print(f"[PHONE-BRIDGE] ❌ Too many keepalive failures, connection likely dead")
                        await self._drop_upstream()
                        consecutive_failures = 0

        except asyncio.CancelledError:
            pass
        finally:
            print("[PHONE-BRIDGE] Health monitor stopped")

    async def _send_keepalive(self):
        """One keepalive frame on the live leg (20 ms of silence)."""
        session = self._ai_session
        adapter = self._leg.adapter
        ws = getattr(session, adapter.ws_attr, None) if session else None
        frame = adapter.keepalive_frame(session) if ws else None
        if frame is not None:
            await ws.send(json.dumps(frame))

    async def _drop_upstream(self):
        """Close the live leg's socket: the listener's reconnect loop re-dials it."""
        session = self._ai_session
        ws = getattr(session, self._leg.adapter.ws_attr, None) if session else None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def stop(self):
        """Stop the AI bridge and cleanup.

        BACKGROUND TASK SUPPORT:
        - For Claude Code backend, if a task is running, we DON'T cancel it
        - Instead, we set _phone_disconnected = True
        - When the task completes, it will trigger a callback
        """
        print(f"[PHONE-BRIDGE] Stopping bridge for {self.phone_session.session_id}")

        # Check if Claude is processing - if so, let it continue in background
        is_claude = self.phone_session.ai_backend == AIBackend.CLAUDE_CODE
        claude_processing = is_claude and getattr(self, '_claude_processing', False)

        if claude_processing:
            print(f"")
            print(f"╔══════════════════════════════════════════════════════════════════╗")
            print(f"║  BACKGROUND TASK ACTIVE - Phone disconnected, Claude continues   ║")
            print(f"╠══════════════════════════════════════════════════════════════════╣")
            print(f"║  Session ID: {self._claude_session_id[:20]}...                      ║")
            print(f"║  Operator: {self._callback_operator:<20}                       ║")
            print(f"║  Callback Number: {self._callback_phone_number:<15}                ║")
            print(f"║  Status: Claude is working, will call back when done             ║")
            print(f"╚══════════════════════════════════════════════════════════════════╝")
            print(f"")
            self._phone_disconnected = True
            self._thinking_music_active = False  # Stop the music

            # Cancel only the VAD processor and keepalive, not the Claude task
            for task in self._tasks:
                # Keep the active Claude task running
                if task == getattr(self, '_active_claude_task', None):
                    print(f"[BACKGROUND] Keeping Claude task alive for callback...")
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        else:
            # No active Claude task - normal cleanup
            self._running = False
            if self._ai_session is not None:
                # The call is over: nothing may re-dial its leg from here on.
                self._ai_session.intentional_disconnect = True

            # Cancel all background tasks
            for task in self._tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._tasks.clear()

        # Save session using existing route's save function
        if self._ai_session:
            # The leg runs the voice bridge's handlers, so its tool calls are
            # session tasks (voice_ws_shared.dispatch_voice_tool): hanging up
            # cancels them, as a portal teardown does. Then the provider socket
            # is closed (it used to outlive the call).
            from Orchestrator.routes.voice_ws_shared import cancel_voice_tools, retire_upstream
            await cancel_voice_tools(self._ai_session, teardown=True)
            if self._leg is not None:
                await retire_upstream(self._ai_session, self._leg.adapter.ws_attr, teardown=True)
            await self._save_session()
            # Evict this call's entry from the live-session registry it was
            # inserted into (REALTIME/GEMINI_LIVE/GROK_LIVE depending on backend).
            # These registries had no removal path — this is the phone leg of the
            # 2026-06-14 live voice-session memory leak. Pop from all three; only
            # the matching backend's dict holds this session_id.
            _sid = getattr(self._ai_session, "session_id", None)
            if _sid:
                REALTIME_SESSIONS.pop(_sid, None)
                GEMINI_LIVE_SESSIONS.pop(_sid, None)
                GROK_LIVE_SESSIONS.pop(_sid, None)

        self.phone_session.status = PhoneStatus.COMPLETED
        self.phone_session.call_end = now_utc_iso()

    _audio_chunks_received = 0
    _audio_chunks_sent = 0

    async def send_audio(self, ulaw_data: bytes):
        """Send phone audio to the AI backend (ULAW path for Twilio)."""
        if not self._running:
            return
        if self.phone_session.ai_backend != AIBackend.CLAUDE_CODE and not self._ai_session:
            return

        self._audio_chunks_received += 1

        if self._audio_chunks_received % 100 == 0:
            print(f"[PHONE-BRIDGE] Audio stats: received={self._audio_chunks_received}, sent={self._audio_chunks_sent}, running={self._running}")

        try:
            if self._leg is not None:
                # ULAW 8 kHz -> PCM16 at the rate the provider session declares.
                await self._send_live_audio(
                    AudioConverter.phone_to_ai(ulaw_data, self._leg.adapter.input_rate))
            elif self.phone_session.ai_backend == AIBackend.CLAUDE_CODE:
                await self._send_claude_audio(ulaw_data)
            self._audio_chunks_sent += 1
        except Exception as e:
            print(f"[PHONE-BRIDGE] Send audio error: {e}")

    async def send_pcm16(self, pcm16_data: bytes, source_rate: int = 16000):
        """Send raw PCM16 audio to the AI backend (cellular — no ULAW).

        All live backends use server-side VAD — send PCM16 directly for best quality.
        """
        if not self._running:
            return
        if self.phone_session.ai_backend != AIBackend.CLAUDE_CODE and not self._ai_session:
            return

        self._audio_chunks_received += 1

        if self._audio_chunks_received % 100 == 0:
            print(f"[PHONE-BRIDGE] PCM16 audio stats: received={self._audio_chunks_received}, sent={self._audio_chunks_sent}")

        try:
            import numpy as np

            if self._leg is not None:
                target_rate = self._leg.adapter.input_rate
                if source_rate != target_rate:
                    from scipy.signal import resample as scipy_resample
                    samples = np.frombuffer(pcm16_data, dtype=np.int16)
                    num_out = int(len(samples) * target_rate / source_rate)
                    resampled = scipy_resample(samples.astype(np.float64), num_out)
                    pcm_out = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
                else:
                    pcm_out = pcm16_data
                await self._send_live_audio(pcm_out)
                self._audio_chunks_sent += 1

            else:
                # Claude Code or unknown — fall back to ULAW path
                samples = np.frombuffer(pcm16_data, dtype=np.int16)
                if source_rate == 16000:
                    if len(samples) >= 2:
                        pcm_8k = ((samples[0::2].astype(np.int32) + samples[1::2].astype(np.int32)) // 2).astype(np.int16).tobytes()
                    else:
                        pcm_8k = pcm16_data
                elif source_rate != 8000:
                    pcm_8k = AudioConverter.downsample(pcm16_data, source_rate // 8000)
                else:
                    pcm_8k = pcm16_data
                ulaw_data = AudioConverter.pcm16_to_ulaw_bytes(pcm_8k)
                await self.send_audio(ulaw_data)

        except Exception as e:
            print(f"[PHONE-BRIDGE] Send PCM16 error: {e}")

    async def _send_live_audio(self, pcm_data: bytes):
        """One chunk of caller audio up the live leg, in the provider's frame.

        Server-side VAD on every provider finds the caller's turns. A send on
        a closed socket is dropped: the listener sees the close and its loop
        re-dials (spawning a reconnect per 20 ms frame here hammered a dead
        provider ~50 times a second, O10-telephony-twilio-09).
        """
        if self._reconnecting:
            return  # Don't send while reconnecting
        session = self._ai_session
        ws = getattr(session, self._leg.adapter.ws_attr, None) if session else None
        if not ws:
            return
        audio_b64 = base64.b64encode(pcm_data).decode('ascii')
        try:
            await ws.send(json.dumps(self._leg.adapter.audio_frame(audio_b64)))
        except websockets.exceptions.ConnectionClosed as e:
            if not self._closed_send_logged:
                self._closed_send_logged = True
                print(f"[PHONE-BRIDGE] {self._leg.adapter.name} connection closed while "
                      f"sending (code={getattr(e.rcvd, 'code', None)}); the listener reconnects")

    # =========================================================================
    # Live voice legs (OpenAI Realtime, Gemini Live, Grok) — the routes' bridges
    # =========================================================================

    async def _start_live(self) -> bool:
        """Start the call's leg on the provider's LiveVoiceBridge.

        The session is a normal live-voice session (tools, context, prompt,
        transcript), driven by the SAME listener and handlers as the Portal
        through a PhoneWebSocketAdapter, with the provider's phone tuning
        (adapter.configure_call). This bridge owns its reconnect loop
        (external_reconnect) and the audio conversion.
        """
        from Orchestrator.voice.bridge import live_bridge

        self._leg = live_bridge(self.phone_session.ai_backend.value)
        adapter = self._leg.adapter

        session_id = f"phone-{self.phone_session.session_id}"
        session = adapter.new_session(session_id)
        session.operator = self.phone_session.operator
        session.status = "connecting"
        # _listener_with_reconnect re-dials this session itself: the voice
        # bridge's listener and goAway handler must not dial a second upstream
        # for it (two dialers raced for the socket on every drop).
        session.external_reconnect = True
        # The phone looks like a client to the voice bridge: audio deltas play on
        # the call, a finished reply's transcript reaches on_ai_transcript.
        self._phone_adapter = PhoneWebSocketAdapter(self)
        session.portal_ws = self._phone_adapter
        adapter.sessions()[session_id] = session
        self._ai_session = session

        # Reconnection state
        self._reconnecting = False
        self._reconnect_count = 0
        self._max_reconnects = 5

        if not await adapter.redial(session):
            print(f"[PHONE-BRIDGE] Failed to connect to {adapter.name}")
            return False

        # Pass custom_role for outbound calls if provided.
        custom_role = getattr(self.phone_session, 'outbound_role', '')
        await adapter.configure_call(session, self.phone_session.operator, custom_role)

        print(f"[PHONE-BRIDGE] {adapter.name} leg up (the Portal's listener via PhoneWebSocketAdapter)")
        task = asyncio.create_task(self._listener_with_reconnect())
        self._tasks.append(task)

        # For outbound calls, trigger the initial response (the AI speaks first).
        is_outbound = getattr(self.phone_session, 'direction', 'inbound') == 'outbound'
        custom_greeting = getattr(self.phone_session, 'outbound_greeting', '')

        if is_outbound:
            # Give the session a moment to stabilize
            await asyncio.sleep(0.5)
            asyncio.create_task(self._send_initial_greeting(custom_greeting))

        return True

    async def _listener_with_reconnect(self):
        """
        Run the voice bridge's listener, and re-dial when the provider drops.

        The Portal's clients reconnect through the route; a call has to stay up
        through a network hiccup on its own. Up to _max_reconnects re-dials per
        call; giving up ends the call.
        """
        adapter = self._leg.adapter
        print(f"[PHONE-BRIDGE] Starting {adapter.name} listener (the Portal's handler)")

        while self._running and self._reconnect_count <= self._max_reconnects:
            try:
                await adapter.listener(self._ai_session)

                # The listener ended: the provider closed the connection.
                if not self._running:
                    break

                print(f"[PHONE-BRIDGE] {adapter.name} listener ended, attempting reconnect...")

            except Exception as e:
                print(f"[PHONE-BRIDGE] {adapter.name} listener error: {type(e).__name__}: {e}")
                if not self._running:
                    break

            if self._running and not self._reconnecting:
                self._reconnecting = True
                self._reconnect_count += 1

                if self._reconnect_count > self._max_reconnects:
                    print(f"[PHONE-BRIDGE] Max {adapter.name} reconnects ({self._max_reconnects}) reached")
                    break

                print(f"[PHONE-BRIDGE] Reconnecting to {adapter.name} ({self._reconnect_count}/{self._max_reconnects})...")

                try:
                    await self._drop_upstream()
                    await asyncio.sleep(1)  # Brief delay before reconnect

                    self._ai_session.status = "reconnecting"
                    if await adapter.redial(self._ai_session):
                        custom_role = getattr(self.phone_session, 'outbound_role', '')
                        await adapter.configure_call(self._ai_session, self.phone_session.operator,
                                                     custom_role)
                        print(f"[PHONE-BRIDGE] {adapter.name} reconnection successful")
                        self._closed_send_logged = False
                        self._reconnecting = False
                        # The loop restarts the listener on the new socket.
                    else:
                        print(f"[PHONE-BRIDGE] {adapter.name} reconnection failed")
                        self._reconnecting = False
                        break

                except Exception as reconnect_err:
                    print(f"[PHONE-BRIDGE] {adapter.name} reconnection error: {reconnect_err}")
                    self._reconnecting = False
                    break

        if not self._reconnecting:
            self._running = False
        print(f"[PHONE-BRIDGE] {adapter.name} listener with reconnect stopped")

    async def _send_initial_greeting(self, custom_greeting: str = ""):
        """Make the AI speak first on an outbound call."""
        adapter = self._leg.adapter
        ws = getattr(self._ai_session, adapter.ws_attr, None) if self._ai_session else None
        if not ws:
            return

        try:
            if custom_greeting:
                prompt = f"The user just answered the phone. Greet them and deliver this message: {custom_greeting}"
            else:
                prompt = f"The user just answered the phone. Introduce yourself briefly and ask how you can help."

            print(f"[PHONE-BRIDGE] Sending {adapter.name} initial greeting prompt: {prompt[:80]}...")
            for frame in adapter.text_turn_frames(f"[SYSTEM: {prompt}]"):
                await ws.send(json.dumps(frame))
            print(f"[PHONE-BRIDGE] {adapter.name} initial greeting triggered")

        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to send {adapter.name} initial greeting: {e}")

    # =========================================================================
    # Claude Code (STT → Text → TTS pipeline using real CLI session)
    # =========================================================================

    async def _start_claude(self) -> bool:
        """Initialize Claude Code CLI session with full MCP tool access.

        Uses the existing PERSISTED_AGENT_SESSIONS system from state.py - the session
        survives phone hangups and can be resumed on callback. This is the SAME
        session system used by the Portal UI agent.

        BACKGROUND TASK SUPPORT:
        - Every Claude turn runs as a background task
        - If the phone disconnects mid-task, Claude continues working
        - When the task completes, if phone is disconnected, Claude calls back automatically
        """
        from Orchestrator.state import PERSISTED_AGENT_SESSIONS, save_operator_state
        from Orchestrator.phone.session import CallDirection
        import uuid

        print("[PHONE-BRIDGE] Starting Claude Code CLI session with background task support...")
        self._claude_audio_buffer = bytearray()
        self._claude_processing = False
        self._claude_process = None
        self._thinking_music_active = False  # Ensure this is reset
        self._phone_disconnected = False  # Track if phone hung up (for callback)
        self._active_claude_task = None  # Track background Claude task
        self._sent_first_message_this_call = False  # Track first message of THIS phone call
        self._tts_playing = False  # Track when TTS is playing (disable VAD during playback)

        # Get or create persistent CLI session for this operator
        operator = self.phone_session.operator
        caller_id = self.phone_session.caller_id

        # Check if a specific session ID was passed (from outbound call with --resume)
        passed_session_id = getattr(self.phone_session, 'claude_session_id', '')

        if passed_session_id:
            # Use the explicitly passed session ID (highest priority)
            self._claude_session_id = passed_session_id
            # Look up message count from persisted sessions if available
            persisted_session = PERSISTED_AGENT_SESSIONS.get(operator)
            if persisted_session and persisted_session.get("claude_session_id") == passed_session_id:
                self._claude_message_count = persisted_session.get("message_count", 0)
            else:
                self._claude_message_count = 0
            self._resume_from_disk = True  # Resume the passed session
            print(f"[PHONE-BRIDGE] Using passed session ID: {self._claude_session_id[:8]}... (resume mode)")
            # Update persisted session to use this session ID
            if operator in PERSISTED_AGENT_SESSIONS:
                PERSISTED_AGENT_SESSIONS[operator]["claude_session_id"] = passed_session_id
                PERSISTED_AGENT_SESSIONS[operator]["last_caller_id"] = caller_id
            else:
                PERSISTED_AGENT_SESSIONS[operator] = {
                    "claude_session_id": passed_session_id,
                    "message_count": self._claude_message_count,
                    "model": "sonnet",
                    "working_directory": str(blackbox_root()),
                    "last_activity": "",
                    "last_caller_id": caller_id
                }
            save_operator_state()
        elif PERSISTED_AGENT_SESSIONS.get(operator):
            # Check if session is still valid (within 15 minute timeout)
            persisted_session = PERSISTED_AGENT_SESSIONS[operator]
            last_activity = persisted_session.get("last_activity", "")
            session_expired = False

            if last_activity:
                try:
                    from datetime import datetime, timezone
                    from Orchestrator.config import PHONE_CLI_SESSION_TIMEOUT_MIN
                    # Parse last_activity timestamp
                    last_time = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    idle_minutes = (now - last_time).total_seconds() / 60

                    # Configurable timeout (default 15 minutes)
                    SESSION_TIMEOUT_MINUTES = PHONE_CLI_SESSION_TIMEOUT_MIN
                    if idle_minutes > SESSION_TIMEOUT_MINUTES:
                        print(f"[PHONE-BRIDGE] Session expired for {operator} (idle {idle_minutes:.1f} min > {SESSION_TIMEOUT_MINUTES} min timeout)")
                        session_expired = True
                    else:
                        print(f"[PHONE-BRIDGE] Session still valid for {operator} (idle {idle_minutes:.1f} min)")
                except Exception as e:
                    print(f"[PHONE-BRIDGE] Could not parse last_activity, treating as valid: {e}")

            if not session_expired:
                # Resume existing session for this operator
                self._claude_session_id = persisted_session.get("claude_session_id")
                self._claude_message_count = persisted_session.get("message_count", 0)
                self._resume_from_disk = True
                # Store caller_id in persisted session for callbacks
                persisted_session["last_caller_id"] = caller_id
                save_operator_state()
                print(f"[PHONE-BRIDGE] Resuming persisted CLI session for {operator}: {self._claude_session_id[:8]}... (msg #{self._claude_message_count})")
            else:
                # Session expired - clear and create new
                del PERSISTED_AGENT_SESSIONS[operator]
                self._claude_session_id = str(uuid.uuid4())
                self._claude_message_count = 0
                self._resume_from_disk = False
                PERSISTED_AGENT_SESSIONS[operator] = {
                    "claude_session_id": self._claude_session_id,
                    "message_count": 0,
                    "model": "sonnet",
                    "working_directory": str(blackbox_root()),
                    "last_activity": "",
                    "last_caller_id": caller_id
                }
                save_operator_state()
                print(f"[PHONE-BRIDGE] Created new CLI session (previous expired) for {operator}: {self._claude_session_id[:8]}...")
        else:
            # Create new session
            self._claude_session_id = str(uuid.uuid4())
            self._claude_message_count = 0
            self._resume_from_disk = False
            # Create persisted session entry
            PERSISTED_AGENT_SESSIONS[operator] = {
                "claude_session_id": self._claude_session_id,
                "message_count": 0,
                "model": "sonnet",
                "working_directory": str(blackbox_root()),
                "last_activity": "",
                "last_caller_id": caller_id
            }
            save_operator_state()
            print(f"[PHONE-BRIDGE] Created new CLI session for {operator}: {self._claude_session_id[:8]}...")

        # Store caller info for callbacks
        self._callback_phone_number = caller_id
        self._callback_operator = operator

        # Phone-specific system prompt for the CLI session
        self._claude_phone_context = f"""You are on a PHONE CALL with {operator}.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses.

═══════════════════════════════════════════════════════════════════════════════
AUTOMATIC CALLBACK ENABLED
═══════════════════════════════════════════════════════════════════════════════
User's Phone Number: {caller_id}
Operator: {operator}

If the user hangs up while you're still working on a task:
- You will CONTINUE working in the background
- When you finish, you will AUTOMATICALLY call them back at {caller_id}
- The callback will resume this same conversation

Tell the user: "I'll keep working on this. Hang up whenever you want and I'll call you back when I'm done."

*** CALLBACK RESPONSE RULES (CRITICAL) ***
When this is a CALLBACK (you called the user back after completing work):
- Deliver your COMPLETE response IMMEDIATELY after the greeting
- Do NOT ask "would you like to hear more?" or "should I continue?"
- Do NOT give a summary/teaser and wait for permission
- The user hung up specifically so you could work - they want the FULL answer NOW
- Speak the entire result in one response, formatted for voice
- Only pause for questions AFTER you've delivered the complete information
═══════════════════════════════════════════════════════════════════════════════

CRITICAL PHONE CONTEXT:
- Your responses will be converted to speech (TTS) - keep them CONCISE and conversational
- Avoid code blocks, markdown, long lists, or anything that doesn't speak well
- Use natural speech patterns and contractions
- This is a voice conversation - be brief and to the point
- You have FULL ACCESS to BlackBox MCP tools (search snapshots, mint, analyze media, etc.)
- You can browse the web, control Chrome, generate images/videos/music
- If asked to create a snapshot, use the appropriate MCP tool

AVAILABLE CAPABILITIES:
- BlackBox memory: search_snapshots, mint_snapshot, get_context
- Media generation: gemini_image/openai_image/grok_image, generate_video, lyria_music
- Web browsing: web_search, web_fetch
- Chrome automation: All claude-in-chrome tools
- TTS/STT: text_to_speech, speech_to_text, analyze_audio

The user's speech is being transcribed and sent to you. Respond conversationally."""

        # Start VAD processor
        task = asyncio.create_task(self._claude_vad_processor())
        self._tasks.append(task)

        # Speak initial greeting
        asyncio.create_task(self._claude_initial_greeting())

        return True

    async def _claude_initial_greeting(self):
        """Speak initial greeting when Claude connects."""
        await asyncio.sleep(0.5)  # Brief delay after ready tone

        from Orchestrator.phone.session import CallDirection

        # Determine greeting based on call direction
        custom_greeting = getattr(self.phone_session, 'outbound_greeting', '')
        is_outbound = self.phone_session.direction == CallDirection.OUTBOUND

        if is_outbound and custom_greeting:
            # Outbound call with custom greeting - use the custom text via TTS
            greeting = custom_greeting
            print(f"[PHONE-BRIDGE] Custom outbound greeting: {greeting[:50]}...")
        elif is_outbound:
            # Outbound call without custom greeting - short default
            greeting = f"Hey {self.phone_session.operator}, it's Claude."
            print(f"[PHONE-BRIDGE] Default outbound greeting: {greeting}")
        else:
            # Inbound call - use generic greeting
            greeting = f"Hello {self.phone_session.operator}, this is Claude. How can I help you today?"
            print(f"[PHONE-BRIDGE] Inbound greeting: {greeting}")

        # Convert greeting to speech
        audio_data = await self._openai_tts(greeting)
        if audio_data and self.on_ai_audio:
            # Disable VAD while TTS plays (prevent interrupting Claude)
            self._tts_playing = True
            try:
                ulaw_data = AudioConverter.ai_to_phone(audio_data, 24000)
                await self.on_ai_audio(ulaw_data)
            finally:
                self._tts_playing = False

        if self.on_ai_transcript:
            await self.on_ai_transcript(greeting)

    async def _send_claude_audio(self, ulaw_data: bytes):
        """Buffer audio for Claude STT processing using WebRTC VAD."""
        # Store raw 8kHz ulaw for VAD processing (VAD needs 8kHz)
        if not hasattr(self, '_claude_vad_queue'):
            self._claude_vad_queue = asyncio.Queue()
        await self._claude_vad_queue.put(ulaw_data)

    async def _claude_vad_processor(self):
        """Process buffered audio for Claude Code using WebRTC VAD."""
        import struct

        # Initialize WebRTC VAD
        if WEBRTCVAD_AVAILABLE:
            vad = webrtcvad.Vad()
            vad.set_mode(3)  # Most aggressive filtering for noisy phone
            print("[PHONE-BRIDGE] Using WebRTC VAD (mode 3)")
        else:
            print("[PHONE-BRIDGE] WebRTC VAD not available - Claude voice disabled")
            return

        # VAD parameters (matching CLI session)
        VAD_FRAME_MS = 20
        VAD_SAMPLE_RATE = 8000
        VAD_FRAME_BYTES = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 320 bytes
        VAD_SILENCE_FRAMES = 150  # 3 seconds (150 * 20ms)
        VAD_MIN_SPEECH_FRAMES = 15  # 300ms minimum
        VAD_PRE_BUFFER_FRAMES = 25  # 500ms pre-buffer

        # State
        is_speaking = False
        speech_frames = 0
        silence_frames = 0
        audio_buffer_8k = bytearray()  # 8kHz PCM16 for accumulating speech
        pre_buffer = []  # Ring buffer before speech detected
        frame_buffer = bytearray()  # Accumulate until full frame

        # Create queue if not exists
        if not hasattr(self, '_claude_vad_queue'):
            self._claude_vad_queue = asyncio.Queue()

        while self._running:
            try:
                # Get audio with timeout
                try:
                    ulaw_data = await asyncio.wait_for(
                        self._claude_vad_queue.get(),
                        timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                # Skip VAD processing while TTS is playing (prevent interrupting Claude's speech)
                if getattr(self, '_tts_playing', False):
                    continue

                # Convert ULAW to PCM16 at 8kHz (for VAD)
                pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ulaw_data)
                frame_buffer.extend(pcm_8k)

                # Process complete frames
                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                    frame_buffer = frame_buffer[VAD_FRAME_BYTES:]

                    # Run VAD on frame
                    try:
                        is_speech = vad.is_speech(frame, VAD_SAMPLE_RATE)
                    except Exception:
                        is_speech = False

                    if not is_speaking:
                        # Maintain pre-buffer
                        pre_buffer.append(frame)
                        if len(pre_buffer) > VAD_PRE_BUFFER_FRAMES:
                            pre_buffer.pop(0)

                        if is_speech:
                            # Speech started
                            is_speaking = True
                            speech_frames = 1
                            silence_frames = 0
                            # Add pre-buffer
                            for pf in pre_buffer:
                                audio_buffer_8k.extend(pf)
                            pre_buffer.clear()
                            audio_buffer_8k.extend(frame)
                            print(f"[PHONE-BRIDGE] VAD: Speech started")
                            # Play listening start chime
                            asyncio.create_task(self._play_listen_start_chime())
                    else:
                        # Currently speaking
                        audio_buffer_8k.extend(frame)

                        if is_speech:
                            speech_frames += 1
                            silence_frames = 0
                        else:
                            silence_frames += 1

                            if silence_frames >= VAD_SILENCE_FRAMES:
                                # End of utterance
                                speech_duration_ms = speech_frames * VAD_FRAME_MS

                                if speech_frames >= VAD_MIN_SPEECH_FRAMES and not self._claude_processing:
                                    print(f"[PHONE-BRIDGE] VAD: Speech ended ({speech_duration_ms}ms)")
                                    self._claude_processing = True

                                    # Play listening stop chime
                                    await self._play_listen_stop_chime()

                                    # Upsample to 24kHz for Whisper
                                    pcm_24k = AudioConverter.upsample(bytes(audio_buffer_8k), 3)
                                    # Create background task - this continues even if phone disconnects
                                    self._active_claude_task = asyncio.create_task(self._process_claude_turn(pcm_24k))
                                else:
                                    print(f"[PHONE-BRIDGE] VAD: Discarded ({speech_duration_ms}ms too short)")

                                # Reset
                                audio_buffer_8k.clear()
                                is_speaking = False
                                speech_frames = 0
                                silence_frames = 0

            except Exception as e:
                print(f"[PHONE-BRIDGE] VAD error: {e}")
                await asyncio.sleep(0.1)

    async def _process_claude_turn(self, pcm_data: bytes):
        """Process a complete turn through Claude pipeline.

        BACKGROUND TASK SUPPORT:
        - This runs as a background task that continues even if phone disconnects
        - If phone disconnects mid-turn, we still complete the turn
        - After completion, if phone is disconnected, we callback with the response
        """
        thinking_task = None
        try:
            # STT
            user_text = await self._whisper_transcribe(pcm_data)
            if not user_text or not user_text.strip():
                self._claude_processing = False
                return

            print(f"[PHONE-BRIDGE] User: {user_text}")

            # Log the user message to conversation
            self.phone_session.add_message("user", user_text, "voice")

            # Start thinking music loop while waiting for Claude (only if phone connected)
            if not self._phone_disconnected:
                self._thinking_music_active = True
                thinking_task = asyncio.create_task(self._play_thinking_loop())

            # Claude response - THIS CONTINUES EVEN IF PHONE DISCONNECTS
            print(f"[PHONE-BRIDGE] Sending to Claude (background task enabled)...")
            ai_text = await self._claude_respond(user_text)

            # Log if we completed while disconnected
            if self._phone_disconnected:
                print(f"[BACKGROUND] Claude finished processing while phone was disconnected")

            # Stop thinking music
            self._thinking_music_active = False
            if thinking_task and not thinking_task.done():
                thinking_task.cancel()
                try:
                    await thinking_task
                except asyncio.CancelledError:
                    pass

            if not ai_text:
                self._claude_processing = False
                return

            print(f"[PHONE-BRIDGE] AI: {ai_text[:100]}...")

            # Log the assistant message
            self.phone_session.add_message("assistant", ai_text, "voice")

            # Check if phone disconnected during processing
            if self._phone_disconnected:
                print(f"")
                print(f"╔══════════════════════════════════════════════════════════════════╗")
                print(f"║  BACKGROUND TASK COMPLETE - Initiating callback                  ║")
                print(f"╠══════════════════════════════════════════════════════════════════╣")
                print(f"║  Claude finished while phone was disconnected                    ║")
                print(f"║  Response length: {len(ai_text)} chars                                      ║")
                print(f"║  Calling back: {self._callback_phone_number:<15}                          ║")
                print(f"╚══════════════════════════════════════════════════════════════════╝")
                print(f"")
                await self._trigger_callback(ai_text)
                return

            # Phone still connected - send audio response
            if self.on_ai_transcript:
                await self.on_ai_transcript(ai_text)

            # TTS - disable VAD while playing (prevent music interrupting Claude)
            audio_data = await self._openai_tts(ai_text)
            if audio_data and self.on_ai_audio:
                self._tts_playing = True
                try:
                    ulaw_data = AudioConverter.ai_to_phone(audio_data, 24000)
                    await self.on_ai_audio(ulaw_data)
                finally:
                    self._tts_playing = False

        except Exception as e:
            print(f"[PHONE-BRIDGE] Claude pipeline error: {e}")
            traceback.print_exc()
        finally:
            # Make sure thinking music is stopped
            self._thinking_music_active = False
            if thinking_task and not thinking_task.done():
                thinking_task.cancel()
            self._claude_processing = False
            self._active_claude_task = None

    async def _trigger_callback(self, response_text: str):
        """Trigger a callback to the user with Claude's response.

        Called when:
        1. User hung up while Claude was processing
        2. Claude finished and has a response to deliver

        The FULL response is delivered in the callback greeting.
        User explicitly wants complete results, not teasers.
        """
        callback_number = getattr(self, '_callback_phone_number', '')
        callback_operator = getattr(self, '_callback_operator', '')
        session_id = getattr(self, '_claude_session_id', '')

        if not callback_number:
            print("[PHONE-BRIDGE] No callback number available - cannot callback")
            return

        print(f"[PHONE-BRIDGE] Initiating callback to {callback_number}...")
        print(f"[PHONE-BRIDGE] Response length: {len(response_text)} chars (delivering in full)")

        # Deliver the FULL response - no truncation
        # TTS can handle long text, and user wants complete results
        greeting = f"Hey {callback_operator}, I finished what you asked. Here's the complete answer. {response_text}"

        try:
            # Call the user back using the same session
            result = await self._initiate_callback(
                phone_number=callback_number,
                greeting=greeting,
                operator=callback_operator,
                session_id=session_id
            )
            if result:
                print(f"[PHONE-BRIDGE] Callback initiated successfully: {result}")
            else:
                print(f"[PHONE-BRIDGE] Callback failed")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Callback error: {e}")
            traceback.print_exc()

    async def _initiate_callback(self, phone_number: str, greeting: str, operator: str, session_id: str) -> Optional[dict]:
        """Initiate callback via the box's call route (Orchestrator.telephony) with session resumption."""
        if not AIOHTTP_AVAILABLE:
            return None

        from Orchestrator.config import orchestrator_url
        from Orchestrator.telephony import call_error, call_result_ok, outbound_call_path
        call_url = orchestrator_url(outbound_call_path())

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    call_url,
                    json={
                        "to": phone_number,
                        "backend": "claude_code",  # Resume with Claude
                        "operator": operator,
                        "greeting": greeting,
                        "claude_session_id": session_id  # Resume same session
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = await resp.json(content_type=None) if resp.status == 200 else None
                    # Both call routes answer 200 {"error": ...} when they refuse.
                    if call_result_ok(result):
                        return result
                    print(f"[PHONE-BRIDGE] Callback API error: {resp.status} - {call_error(result, resp.status)}")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Callback request error: {e}")
        return None

    async def _play_thinking_loop(self):
        """Play hold music while Claude is processing."""
        try:
            # Load the hold music
            hold_music = self._generate_hold_music()
            duration_seconds = len(hold_music) / 8000.0  # 8kHz ULAW = 1 byte per sample
            print(f"[PHONE-BRIDGE] Starting hold music ({len(hold_music)} bytes, {duration_seconds:.1f}s loop)")

            # Play the loop - the audio callback handles real-time pacing
            # We wait for the duration minus a small overlap for seamless looping
            while self._thinking_music_active and self.on_ai_audio:
                await self.on_ai_audio(hold_music)
                # Wait for the actual duration (the send function paces the audio)
                # Subtract 0.5s for overlap to ensure seamless loop
                wait_time = max(duration_seconds - 0.5, 1.0)
                await asyncio.sleep(wait_time)

        except asyncio.CancelledError:
            print("[PHONE-BRIDGE] Hold music stopped")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Hold music error: {e}")

    async def _whisper_transcribe(self, pcm_data: bytes) -> Optional[str]:
        """Transcribe audio using Whisper."""
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_STT_URL, STT_MODEL

        if not AIOHTTP_AVAILABLE or not OPENAI_API_KEY:
            return None

        try:
            import io
            import wave

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm_data)

            wav_buffer.seek(0)

            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field('file', wav_buffer, filename='audio.wav', content_type='audio/wav')
                form.add_field('model', STT_MODEL)

                async with session.post(
                    OPENAI_STT_URL,
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("text", "")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Whisper error: {e}")
        return None

    async def _claude_respond(self, user_text: str) -> Optional[str]:
        """Get response from Claude Code CLI session with full MCP tool access.

        Runs on the shared CliAgentTransport (audit O10-telephony-twilio-06): it
        reads whole stream-json lines without blocking the event loop (the old
        select()+readline() on a non-blocking pipe dropped any event split across
        pipe writes, so a long answer came back empty), drains stderr, and stops
        the CLI's whole process tree on the timeout.
        """
        import uuid
        from Orchestrator.cli_agent.transport import OUT, CliAgentTransport
        from Orchestrator.routes.agent_routes import claude_conversation_missing, claude_spawn_spec
        from Orchestrator.state import PERSISTED_AGENT_SESSIONS, save_operator_state
        from Orchestrator.volume import now_utc_iso

        try:
            self._claude_message_count += 1
            operator = self.phone_session.operator

            # Track if this is the first message in THIS phone call
            # (separate from total message count which persists across calls)
            is_first_message_this_call = not getattr(self, '_sent_first_message_this_call', False)
            self._sent_first_message_this_call = True

            # Determine session mode:
            # - is_new_session: Brand new session (first call ever)
            # - is_resume: Resuming an existing session (callback or reconnect)
            # - is_continuation: Subsequent messages within this phone call
            is_new_session = is_first_message_this_call and not self._resume_from_disk
            is_resume = is_first_message_this_call and self._resume_from_disk
            is_continuation = not is_first_message_this_call

            # Build the prompt - include phone context on first message of this call
            if is_first_message_this_call:
                prompt = f"{self._claude_phone_context}\n\nUser says: {user_text}"
            else:
                prompt = user_text

            # Every turn names this operator's conversation by id: --session-id
            # starts it, --resume continues it. Never --continue, which resumes the
            # most recent conversation in the shared working directory, possibly
            # another operator's (audit O06-frontier-agent-13).
            working_dir = str(blackbox_root())
            spec = claude_spawn_spec(
                working_dir,
                prompt,
                claude_session_id=self._claude_session_id,
                resume_session=not is_new_session,
                model="sonnet",  # Use sonnet for faster responses
                permission_mode="yolo"
            )

            async def new_conversation_if_missing(returncode: int, diagnostics: str):
                """A resume whose conversation the CLI does not have (an id saved
                for a call that hung up before any turn ran, or a transcript Claude
                Code cleaned up) starts a new conversation in this same turn, so
                the caller is answered and later turns continue the new one."""
                if not claude_conversation_missing(diagnostics):
                    return None
                lost = self._claude_session_id or ""
                self._claude_session_id = str(uuid.uuid4())
                self._claude_message_count = 1
                self._resume_from_disk = False
                print(f"[PHONE-BRIDGE] Conversation {lost[:8]}... is gone (rc={returncode}); "
                      f"starting {self._claude_session_id[:8]}...")
                if operator in PERSISTED_AGENT_SESSIONS:
                    PERSISTED_AGENT_SESSIONS[operator]["claude_session_id"] = self._claude_session_id
                    PERSISTED_AGENT_SESSIONS[operator]["message_count"] = 1
                    save_operator_state()
                fresh_prompt = (prompt if is_first_message_this_call
                                else f"{self._claude_phone_context}\n\nUser says: {user_text}")
                fresh = claude_spawn_spec(
                    working_dir,
                    fresh_prompt,
                    claude_session_id=self._claude_session_id,
                    resume_session=False,
                    model="sonnet",
                    permission_mode="yolo"
                )
                return fresh, "The earlier conversation is gone; starting a new one."

            def note_session_id(line: str) -> None:
                """Follow the conversation id the CLI reports (init/result events).
                The transport's on_line hook: it runs as each line is read, so
                before new_conversation_if_missing, and an error result (the
                failed attempt's) changes nothing."""
                if '"session_id"' not in line:
                    return
                try:
                    event = json.loads(line)
                except ValueError:
                    return
                if (not isinstance(event, dict) or event.get("type") not in ("system", "result")
                        or event.get("is_error")):
                    return
                reported = event.get("session_id")
                if isinstance(reported, str) and reported and reported != self._claude_session_id:
                    print(f"[PHONE-BRIDGE] Claude reports conversation {reported[:8]}...")
                    self._claude_session_id = reported
                    if operator in PERSISTED_AGENT_SESSIONS:
                        PERSISTED_AGENT_SESSIONS[operator]["claude_session_id"] = reported
                        save_operator_state()

            transport = CliAgentTransport(f"phone-{self.phone_session.session_id}",
                                          on_line=note_session_id)
            self._claude_process = transport
            start_seq = await transport.start(
                spec, on_failure=None if is_new_session else new_conversation_if_missing)

            # After resume, switch to continuation mode for subsequent messages
            if is_resume:
                self._resume_from_disk = False

            # Update persisted session
            if operator in PERSISTED_AGENT_SESSIONS:
                PERSISTED_AGENT_SESSIONS[operator]["message_count"] = self._claude_message_count
                PERSISTED_AGENT_SESSIONS[operator]["last_activity"] = now_utc_iso()
                save_operator_state()

            print(f"[PHONE-BRIDGE] Claude CLI started (msg #{self._claude_message_count}, new={is_new_session}, resume={is_resume}, continue={is_continuation})")

            loop = asyncio.get_running_loop()
            start_time = loop.time()
            timeout = 120  # 2 minute max
            state = {"text": "", "tools": 0, "last_log": start_time}

            async def collect() -> None:
                async for kind, payload in transport.follow(start_seq):
                    now = loop.time()
                    # Periodic status logging (every 10 seconds) when phone disconnected
                    if self._phone_disconnected and now - state["last_log"] >= 10:
                        state["last_log"] = now
                        print(f"[BACKGROUND] Claude still working... ({now - start_time:.0f}s elapsed, {len(state['text'])} chars collected, {state['tools']} tool calls)")
                    if kind != OUT:
                        if isinstance(payload, str):
                            print(f"[PHONE-BRIDGE] Claude CLI: {payload[:300]}")
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type", "")

                    if event_type == "assistant":
                        # Collect text content; tool uses are content blocks here
                        message = event.get("message", {})
                        content = message.get("content", []) if isinstance(message, dict) else []
                        for block in content if isinstance(content, list) else []:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                state["text"] += block.get("text", "")
                            elif block.get("type") == "tool_use":
                                state["tools"] += 1
                                if self._phone_disconnected:
                                    print(f"[BACKGROUND] Claude using tool: {block.get('name', 'unknown')}")

                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            state["text"] += delta.get("text", "")

                    elif event_type == "result":
                        if event.get("is_error"):
                            # A failed attempt: its fallback (a new conversation)
                            # or the turn's end follows.
                            continue
                        # Final result
                        result = event.get("result", "")
                        if result and not state["text"] and isinstance(result, str):
                            state["text"] = result
                        return

            try:
                await asyncio.wait_for(collect(), timeout=timeout)
            except asyncio.TimeoutError:
                print("[PHONE-BRIDGE] Claude CLI timeout")
            finally:
                # The CLI exits right after its result; give it a moment, then
                # stop it and every tool it started.
                await transport.finish_or_stop(2)

            response_text = state["text"]
            print(f"[PHONE-BRIDGE] Claude responded: {response_text[:100]}...")
            return response_text.strip() if response_text else None

        except Exception as e:
            print(f"[PHONE-BRIDGE] Claude CLI error: {e}")
            import traceback
            traceback.print_exc()
        return None

    async def _openai_tts(self, text: str) -> Optional[bytes]:
        """Convert text to speech using OpenAI TTS."""
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

        if not AIOHTTP_AVAILABLE or not OPENAI_API_KEY:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OPENAI_TTS_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "tts-1-hd",
                        "voice": "onyx",
                        "input": text,
                        "response_format": "pcm"
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as e:
            print(f"[PHONE-BRIDGE] TTS error: {e}")
        return None

    # =========================================================================
    # Session Saving
    # =========================================================================

    async def _save_session(self):
        """Save the call's transcript with its provider route's save function.

        Non-blocking: uses timeout to prevent cleanup from hanging if /chat is slow.
        """
        if not self._ai_session or self._leg is None:
            return

        try:
            await asyncio.wait_for(self._leg.adapter.save(self._ai_session), timeout=10.0)
            print(f"[PHONE-BRIDGE] Session saved to BlackBox")

        except asyncio.TimeoutError:
            print(f"[PHONE-BRIDGE] Session save timed out after 10s (conversation may not be captured)")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Error saving session: {e}")


async def save_session_to_blackbox(session: PhoneSession):
    """
    Legacy function - now handled by _save_session in PhoneAIBridge.
    Kept for compatibility with twilio_routes.py.
    """
    # The bridge now handles saving through existing route functions
    pass
