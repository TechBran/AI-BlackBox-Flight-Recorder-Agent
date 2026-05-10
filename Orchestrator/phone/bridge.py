#!/usr/bin/env python3
"""
bridge.py - Phone to AI WebSocket Bridge

Bridges phone audio to AI backends by reusing the existing WebSocket route handlers.
This ensures the phone works exactly like the Portal UI.

Audio Flow:
    Twilio (8kHz ULAW) → Convert → Existing WebSocket Routes → AI Backend
    AI Backend → Existing WebSocket Routes → Convert → Twilio (8kHz ULAW)
"""

import asyncio
import json
import base64
import traceback
from typing import Optional, Dict, Any, Callable, Awaitable

from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    AIBackend,
)
from Orchestrator.phone.audio_converter import AudioConverter
from Orchestrator.utils.paths import blackbox_root
from Orchestrator.volume import now_utc_iso

# Import existing session models
from Orchestrator.models import (
    RealtimeSession,
    GeminiLiveSession,
    GrokLiveSession,
    REALTIME_SESSIONS,
    GEMINI_LIVE_SESSIONS,
    GROK_LIVE_SESSIONS,
)

# WebSocket library
try:
    import websockets
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

# WebRTC VAD for robust voice activity detection
try:
    import webrtcvad
    WEBRTCVAD_AVAILABLE = True
except ImportError:
    WEBRTCVAD_AVAILABLE = False
    print("[PHONE-BRIDGE] webrtcvad not installed - using energy-based VAD")
    print("[PHONE-BRIDGE] aiohttp library not installed")


class PhoneWebSocketAdapter:
    """
    Adapter that makes phone bridge look like a Portal WebSocket.

    This allows reusing the proven Portal message handlers (like handle_gemini_message)
    by intercepting send_json() calls and routing them appropriately:
    - audio_delta: Convert 24kHz PCM16 → 8kHz µ-law and send to Twilio
    - transcript events: Forward to transcript callback
    - other events: Log for debugging

    This ensures phone calls use the EXACT same code path as the Portal UI.
    """

    def __init__(self, bridge: 'PhoneAIBridge'):
        self.bridge = bridge
        self._is_speaking = False
        self._transcript_buffer = ""

    async def send_json(self, data: dict):
        """
        Handle messages that would normally go to Portal WebSocket.
        Routes audio to phone, logs other events.

        Also updates _last_ai_message_time for health monitoring.
        """
        import time
        msg_type = data.get("type", "")

        # Update health monitor timestamp - Gemini is alive if we're receiving messages
        self.bridge._last_ai_message_time = time.time()

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
                    self.bridge._gemini_is_speaking = True
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
            # Tell VAD processor that Gemini stopped speaking (start grace period)
            self.bridge._gemini_is_speaking = False
            self.bridge._gemini_speech_end_time = time.time()

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
            # User's transcribed speech from Whisper
            transcript = data.get("data", "")
            if transcript:
                print(f"[PHONE-ADAPTER] User said: {transcript[:100]}...")

        elif msg_type == "setup_complete":
            print(f"[PHONE-ADAPTER] Gemini setup complete")

        elif msg_type == "connected":
            print(f"[PHONE-ADAPTER] Connected to Gemini")

        elif msg_type == "tool_call":
            tool_data = data.get("data", {})
            print(f"[PHONE-ADAPTER] Tool call: {tool_data.get('name', 'unknown')}")

        elif msg_type == "tool_result":
            tool_data = data.get("data", {})
            print(f"[PHONE-ADAPTER] Tool result: {tool_data.get('name', 'unknown')}")

        elif msg_type == "error":
            error_msg = data.get("data", "Unknown error")
            print(f"[PHONE-ADAPTER] Error from Gemini: {error_msg}")

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
    Bridges a phone call to an AI backend using existing WebSocket route handlers.

    Instead of reimplementing WebSocket logic, this class:
    1. Creates proper session objects (RealtimeSession, GeminiLiveSession, etc.)
    2. Uses existing connect/configure functions from the route modules
    3. Converts audio between phone (8kHz ULAW) and AI (16-24kHz PCM16) formats
    4. Intercepts audio output and sends to Twilio

    GEMINI REFACTOR (2026-02):
    For Gemini Live, we now use a PhoneWebSocketAdapter that allows reusing the
    proven Portal message handler (handle_gemini_message) instead of a custom
    phone-specific listener. This ensures identical behavior to the Portal UI.
    """

    def __init__(self, phone_session: PhoneSession):
        self.phone_session = phone_session
        self._running = False
        self._tasks: list = []

        # The actual AI session (RealtimeSession, GeminiLiveSession, or GrokLiveSession)
        self._ai_session: Optional[Any] = None

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
        """Start the AI bridge using existing WebSocket route handlers."""
        if not WEBSOCKETS_AVAILABLE:
            print("[PHONE-BRIDGE] Cannot start - websockets not available")
            return False

        self.phone_session.operator = operator or self.phone_session.operator or "phone-caller"
        self._running = True

        try:
            backend = self.phone_session.ai_backend
            print(f"[PHONE-BRIDGE] Starting bridge for {backend.value} with operator {self.phone_session.operator}")

            if backend == AIBackend.OPENAI_REALTIME:
                success = await self._start_openai()
            elif backend == AIBackend.GEMINI_LIVE:
                success = await self._start_gemini()
            elif backend == AIBackend.GROK_LIVE:
                success = await self._start_grok()
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
        Send periodic keepalive AND monitor connection health.

        This loop:
        1. Sends silent audio to prevent AI backend timeout
        2. Tracks consecutive send failures
        3. Monitors last message received time (stale connection detection)
        4. Triggers reconnection if connection appears dead
        """
        import time

        keepalive_interval = 10  # seconds
        backend = self.phone_session.ai_backend
        consecutive_failures = 0
        max_consecutive_failures = 3
        stale_timeout = 60  # seconds without receiving a message = stale

        # Initialize last message time tracking
        if not hasattr(self, '_last_ai_message_time'):
            self._last_ai_message_time = time.time()

        print(f"[PHONE-BRIDGE] Health monitor started (keepalive={keepalive_interval}s, stale={stale_timeout}s)")

        try:
            while self._running:
                await asyncio.sleep(keepalive_interval)

                if not self._running:
                    break

                # Check for stale connection (no messages received recently)
                time_since_message = time.time() - self._last_ai_message_time
                if time_since_message > stale_timeout:
                    print(f"[PHONE-BRIDGE] ⚠️ STALE CONNECTION: No AI message for {time_since_message:.0f}s")
                    print(f"[PHONE-BRIDGE] Triggering reconnection due to stale connection...")

                    # Trigger reconnection based on backend
                    if backend == AIBackend.OPENAI_REALTIME:
                        if not getattr(self, '_openai_reconnecting', False):
                            asyncio.create_task(self._reconnect_openai())
                    elif backend == AIBackend.GEMINI_LIVE:
                        # Gemini uses the listener_with_reconnect which handles this
                        pass
                    elif backend == AIBackend.GROK_LIVE:
                        if not getattr(self, '_grok_reconnecting', False):
                            asyncio.create_task(self._reconnect_grok())

                    # Reset timer after triggering reconnect
                    self._last_ai_message_time = time.time()
                    consecutive_failures = 0
                    continue

                # Send keepalive
                try:
                    if backend == AIBackend.OPENAI_REALTIME:
                        await self._send_openai_keepalive()
                    elif backend == AIBackend.GEMINI_LIVE:
                        await self._send_gemini_keepalive()
                    elif backend == AIBackend.GROK_LIVE:
                        await self._send_grok_keepalive()
                    # Claude doesn't need keepalive (text-based)

                    # Reset failure count on success
                    if consecutive_failures > 0:
                        print(f"[PHONE-BRIDGE] Keepalive recovered after {consecutive_failures} failures")
                    consecutive_failures = 0

                except websockets.exceptions.ConnectionClosed as e:
                    print(f"[PHONE-BRIDGE] ❌ Keepalive failed - connection closed: {e.code}")
                    self._running = False
                    break

                except Exception as e:
                    consecutive_failures += 1
                    print(f"[PHONE-BRIDGE] ⚠️ Keepalive error ({consecutive_failures}/{max_consecutive_failures}): {e}")

                    if consecutive_failures >= max_consecutive_failures:
                        print(f"[PHONE-BRIDGE] ❌ Too many keepalive failures, connection likely dead")

                        # Trigger reconnection
                        if backend == AIBackend.OPENAI_REALTIME:
                            if not getattr(self, '_openai_reconnecting', False):
                                asyncio.create_task(self._reconnect_openai())
                        elif backend == AIBackend.GROK_LIVE:
                            if not getattr(self, '_grok_reconnecting', False):
                                asyncio.create_task(self._reconnect_grok())

                        consecutive_failures = 0

        except asyncio.CancelledError:
            pass
        finally:
            print("[PHONE-BRIDGE] Health monitor stopped")

    async def _send_openai_keepalive(self):
        """Send keepalive to OpenAI."""
        if self._ai_session and self._ai_session.openai_ws:
            try:
                # Send a tiny amount of silence (20ms)
                silence = AudioConverter.generate_silence(duration_ms=20, sample_rate=8000)
                pcm_data = AudioConverter.phone_to_ai(silence, 24000)
                audio_b64 = base64.b64encode(pcm_data).decode('ascii')
                await self._ai_session.openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64
                }))
            except websockets.exceptions.ConnectionClosed:
                self._running = False

    async def _send_gemini_keepalive(self):
        """Send keepalive to Gemini."""
        if self._ai_session and self._ai_session.gemini_ws:
            try:
                # Send a tiny amount of silence
                silence = AudioConverter.generate_silence(duration_ms=20, sample_rate=8000)
                pcm_data = AudioConverter.phone_to_ai(silence, 16000)
                audio_b64 = base64.b64encode(pcm_data).decode('ascii')
                await self._ai_session.gemini_ws.send(json.dumps({
                    "realtimeInput": {
                        "mediaChunks": [{
                            "mimeType": "audio/pcm;rate=16000",
                            "data": audio_b64
                        }]
                    }
                }))
            except websockets.exceptions.ConnectionClosed:
                self._running = False

    async def _send_grok_keepalive(self):
        """Send keepalive to Grok."""
        if self._ai_session and self._ai_session.grok_ws:
            try:
                # Send a tiny amount of silence
                silence = AudioConverter.generate_silence(duration_ms=20, sample_rate=8000)
                pcm_data = AudioConverter.phone_to_ai(silence, 24000)
                audio_b64 = base64.b64encode(pcm_data).decode('ascii')
                await self._ai_session.grok_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64
                }))
            except websockets.exceptions.ConnectionClosed:
                self._running = False

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
            await self._save_session()

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
        backend = self.phone_session.ai_backend

        if self._audio_chunks_received % 100 == 0:
            print(f"[PHONE-BRIDGE] Audio stats: received={self._audio_chunks_received}, sent={self._audio_chunks_sent}, running={self._running}")

        try:
            if backend == AIBackend.OPENAI_REALTIME:
                await self._send_openai_audio(ulaw_data)
            elif backend == AIBackend.GEMINI_LIVE:
                await self._send_gemini_audio(ulaw_data)
            elif backend == AIBackend.GROK_LIVE:
                await self._send_grok_audio(ulaw_data)
            elif backend == AIBackend.CLAUDE_CODE:
                await self._send_claude_audio(ulaw_data)
            self._audio_chunks_sent += 1
        except Exception as e:
            print(f"[PHONE-BRIDGE] Send audio error: {e}")

    async def send_pcm16(self, pcm16_data: bytes, source_rate: int = 16000):
        """Send raw PCM16 audio to the AI backend (cellular — no ULAW).

        All backends use server-side VAD — send PCM16 directly for best quality.
        """
        if not self._running:
            return
        if self.phone_session.ai_backend != AIBackend.CLAUDE_CODE and not self._ai_session:
            return

        self._audio_chunks_received += 1
        backend = self.phone_session.ai_backend

        if self._audio_chunks_received % 100 == 0:
            print(f"[PHONE-BRIDGE] PCM16 audio stats: received={self._audio_chunks_received}, sent={self._audio_chunks_sent}")

        try:
            import numpy as np
            from scipy.signal import resample as scipy_resample

            if backend == AIBackend.OPENAI_REALTIME:
                # OpenAI: 24kHz PCM16
                target_rate = 24000
                if source_rate != target_rate:
                    samples = np.frombuffer(pcm16_data, dtype=np.int16)
                    num_out = int(len(samples) * target_rate / source_rate)
                    resampled = scipy_resample(samples.astype(np.float64), num_out)
                    pcm_out = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
                else:
                    pcm_out = pcm16_data

                audio_b64 = base64.b64encode(pcm_out).decode('ascii')
                if self._ai_session.openai_ws:
                    await self._ai_session.openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64
                    }))
                self._audio_chunks_sent += 1

            elif backend == AIBackend.GROK_LIVE:
                # Grok: 24kHz PCM16 (same format as OpenAI)
                target_rate = 24000
                if source_rate != target_rate:
                    samples = np.frombuffer(pcm16_data, dtype=np.int16)
                    num_out = int(len(samples) * target_rate / source_rate)
                    resampled = scipy_resample(samples.astype(np.float64), num_out)
                    pcm_out = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
                else:
                    pcm_out = pcm16_data

                audio_b64 = base64.b64encode(pcm_out).decode('ascii')
                if self._ai_session.grok_ws:
                    await self._ai_session.grok_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64
                    }))
                self._audio_chunks_sent += 1

            elif backend == AIBackend.GEMINI_LIVE:
                # Gemini: 16kHz PCM16
                target_rate = 16000
                if source_rate != target_rate:
                    samples = np.frombuffer(pcm16_data, dtype=np.int16)
                    num_out = int(len(samples) * target_rate / source_rate)
                    resampled = scipy_resample(samples.astype(np.float64), num_out)
                    pcm_out = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
                else:
                    pcm_out = pcm16_data

                audio_b64 = base64.b64encode(pcm_out).decode('ascii')
                if self._ai_session.gemini_ws:
                    await self._ai_session.gemini_ws.send(json.dumps({
                        "realtimeInput": {
                            "mediaChunks": [{
                                "mimeType": "audio/pcm;rate=16000",
                                "data": audio_b64
                            }]
                        }
                    }))
                self._audio_chunks_sent += 1

            else:
                # Claude Code or unknown — fall back to ULAW path
                from Orchestrator.phone.audio_converter import AudioConverter
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

    # =========================================================================
    # OpenAI Realtime - Uses existing realtime_routes.py
    # =========================================================================

    async def _start_openai(self) -> bool:
        """Start OpenAI using existing route handlers."""
        from Orchestrator.routes.realtime_routes import (
            connect_to_openai,
            configure_openai_session,
        )

        # Create a RealtimeSession (same as Portal does)
        session_id = f"phone-{self.phone_session.session_id}"
        self._ai_session = RealtimeSession(
            session_id=session_id,
            operator=self.phone_session.operator,
            status="connecting",
        )
        REALTIME_SESSIONS[session_id] = self._ai_session

        # Track reconnection state
        self._openai_reconnecting = False
        self._openai_reconnect_count = 0
        self._openai_max_reconnects = 5

        # Track AI speaking state for VAD muting
        self._openai_is_speaking = False
        self._openai_speech_end_time = None

        # Connect using existing function
        success = await connect_to_openai(self._ai_session)
        if not success:
            return False

        # Configure using existing function (includes tools, context, etc.)
        # Pass custom_role for outbound calls if provided
        custom_role = getattr(self.phone_session, 'outbound_role', '')
        await configure_openai_session(
            self._ai_session,
            self.phone_session.operator,
            voice="ash",
            custom_role=custom_role
        )

        # Start our own listener that converts audio for phone
        task = asyncio.create_task(self._openai_phone_listener())
        self._tasks.append(task)

        # NOTE: No client-side VAD for OpenAI phone path.
        # OpenAI's server-side VAD handles turn detection well for phone audio.
        # Whisper hallucinations are caught by the is_whisper_hallucination() filter.

        # For outbound calls with a greeting, trigger initial response
        is_outbound = getattr(self.phone_session, 'direction', 'inbound') == 'outbound'
        custom_greeting = getattr(self.phone_session, 'outbound_greeting', '')

        if is_outbound:
            # Give the session a moment to stabilize
            await asyncio.sleep(0.5)
            asyncio.create_task(self._send_openai_initial_greeting(custom_greeting))

        return True

    async def _send_openai_initial_greeting(self, custom_greeting: str = ""):
        """Send an initial greeting to trigger OpenAI response for outbound calls."""
        if not self._ai_session or not self._ai_session.openai_ws:
            return

        try:
            operator = self.phone_session.operator

            # Create a system context message to prompt the AI to speak first
            if custom_greeting:
                prompt = f"The user just answered the phone. Greet them and deliver this message: {custom_greeting}"
            else:
                prompt = f"The user just answered the phone. Introduce yourself briefly and ask how you can help."

            print(f"[PHONE-BRIDGE] Sending OpenAI initial greeting prompt: {prompt[:80]}...")

            # Send as a conversation item
            await self._ai_session.openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": f"[SYSTEM: {prompt}]"
                    }]
                }
            }))

            # Request immediate response
            await self._ai_session.openai_ws.send(json.dumps({
                "type": "response.create"
            }))

            print(f"[PHONE-BRIDGE] OpenAI initial greeting triggered")

        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to send initial greeting: {e}")

    async def _reconnect_openai(self) -> bool:
        """Attempt to reconnect to OpenAI after connection drop."""
        from Orchestrator.routes.realtime_routes import (
            connect_to_openai,
            configure_openai_session,
        )

        if self._openai_reconnecting:
            return False  # Already reconnecting

        self._openai_reconnect_count += 1
        if self._openai_reconnect_count > self._openai_max_reconnects:
            print(f"[PHONE-BRIDGE] Max reconnection attempts ({self._openai_max_reconnects}) reached, giving up")
            return False

        self._openai_reconnecting = True
        print(f"[PHONE-BRIDGE] Attempting OpenAI reconnection ({self._openai_reconnect_count}/{self._openai_max_reconnects})...")

        try:
            # Close old connection if exists
            if self._ai_session and self._ai_session.openai_ws:
                try:
                    await self._ai_session.openai_ws.close()
                except:
                    pass

            # Brief delay before reconnect
            await asyncio.sleep(1)

            # Reconnect
            self._ai_session.status = "reconnecting"
            success = await connect_to_openai(self._ai_session)
            if not success:
                print("[PHONE-BRIDGE] Reconnection failed - could not connect")
                self._openai_reconnecting = False
                return False

            # Reconfigure session
            custom_role = getattr(self.phone_session, 'outbound_role', '')
            await configure_openai_session(
                self._ai_session,
                self.phone_session.operator,
                voice="ash",
                custom_role=custom_role
            )

            # Start new listener
            task = asyncio.create_task(self._openai_phone_listener())
            self._tasks.append(task)

            print(f"[PHONE-BRIDGE] OpenAI reconnected successfully!")
            self._openai_reconnecting = False
            return True

        except Exception as e:
            print(f"[PHONE-BRIDGE] Reconnection error: {e}")
            self._openai_reconnecting = False
            return False

    async def _send_openai_audio(self, ulaw_data: bytes):
        """Convert and send audio directly to OpenAI.

        Unlike Gemini/Grok, OpenAI's server-side VAD handles turn detection well.
        We rely on the Whisper hallucination filter (whisper_filter.py) to catch
        false transcripts rather than gating audio with a client-side VAD.
        Phone audio characteristics make client-side VAD unreliable for OpenAI.
        """
        if not self._ai_session or not self._ai_session.openai_ws:
            return

        if self._openai_reconnecting:
            return  # Don't send while reconnecting

        try:
            # Convert ULAW 8kHz to PCM16 24kHz
            pcm_data = AudioConverter.phone_to_ai(ulaw_data, 24000)
            audio_b64 = base64.b64encode(pcm_data).decode('ascii')

            await self._ai_session.openai_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": audio_b64
            }))
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] OpenAI connection closed while sending: code={e.code}, reason={e.reason}")
            asyncio.create_task(self._reconnect_openai())

    async def _openai_vad_processor(self):
        """Process audio for OpenAI using WebRTC VAD.

        Gates phone audio so only speech reaches OpenAI's server-side VAD/Whisper.
        Prevents Whisper hallucinations ("Thank you", Chinese text, etc.) on silence.
        """
        import struct
        import time

        # Initialize WebRTC VAD
        if WEBRTCVAD_AVAILABLE:
            vad = webrtcvad.Vad()
            vad.set_mode(2)  # Mode 2: balanced
            print("[PHONE-BRIDGE] OpenAI VAD processor started (WebRTC mode 2)")
        else:
            print("[PHONE-BRIDGE] WebRTC VAD not available - falling back to direct send for OpenAI")
            await self._openai_direct_send_fallback()
            return

        # VAD parameters — tuned for phone audio (noisier than browser mic)
        VAD_FRAME_MS = 20
        VAD_SAMPLE_RATE = 8000
        VAD_FRAME_BYTES = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 320 bytes
        VAD_SILENCE_FRAMES = 40  # 800ms of silence → stop sending (matches Gemini VAD)
        VAD_MIN_SPEECH_FRAMES = 10  # 200ms minimum speech to send
        VAD_SPEECH_ACTIVATE_FRAMES = 8  # 160ms consecutive speech to activate (prevents noise spikes)
        VAD_PRE_BUFFER_FRAMES = 15  # 300ms pre-buffer to catch speech onset
        VAD_GRACE_PERIOD = 0.8  # 800ms grace after AI finishes speaking
        VAD_ENERGY_THRESHOLD = 600  # RMS energy threshold (raised from 400 for phone noise floor)

        # State
        is_speaking = False
        speech_frames = 0
        silence_frames = 0
        pre_buffer = []
        frame_buffer = bytearray()

        # Track AI speaking state
        if not hasattr(self, '_openai_is_speaking'):
            self._openai_is_speaking = False
        if not hasattr(self, '_openai_speech_end_time'):
            self._openai_speech_end_time = None

        # Create queue if not exists
        if not hasattr(self, '_openai_vad_queue'):
            self._openai_vad_queue = asyncio.Queue()

        while self._running:
            try:
                try:
                    ulaw_data = await asyncio.wait_for(
                        self._openai_vad_queue.get(),
                        timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                # Convert ULAW to PCM16 at 8kHz for VAD analysis
                pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ulaw_data)
                frame_buffer.extend(pcm_8k)

                # Mute VAD while AI is speaking + grace period
                vad_muted = False
                if self._openai_is_speaking:
                    vad_muted = True
                elif self._openai_speech_end_time:
                    elapsed = time.time() - self._openai_speech_end_time
                    if elapsed < VAD_GRACE_PERIOD:
                        vad_muted = True
                    else:
                        self._openai_speech_end_time = None

                if vad_muted:
                    frame_buffer.clear()
                    if is_speaking:
                        is_speaking = False
                        speech_frames = 0
                        silence_frames = 0
                        pre_buffer.clear()
                    continue

                # Process complete VAD frames
                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                    frame_buffer = frame_buffer[VAD_FRAME_BYTES:]

                    # Calculate RMS energy
                    samples = struct.unpack(f"<{len(frame)//2}h", frame)
                    rms_energy = (sum(s*s for s in samples) / len(samples)) ** 0.5

                    # Determine if speech
                    if rms_energy < VAD_ENERGY_THRESHOLD:
                        is_speech = False
                    else:
                        try:
                            is_speech = vad.is_speech(frame, VAD_SAMPLE_RATE)
                        except Exception:
                            is_speech = False

                    if not is_speaking:
                        # Pre-buffer frames
                        pre_buffer.append(frame)
                        if len(pre_buffer) > VAD_PRE_BUFFER_FRAMES:
                            pre_buffer.pop(0)

                        if is_speech:
                            speech_frames += 1
                            if speech_frames >= VAD_SPEECH_ACTIVATE_FRAMES:
                                is_speaking = True
                                silence_frames = 0

                                # Send pre-buffer + current frame
                                for pf in pre_buffer:
                                    pcm_24k = AudioConverter.phone_to_ai(pf, 24000)
                                    audio_b64 = base64.b64encode(pcm_24k).decode('ascii')
                                    try:
                                        await self._ai_session.openai_ws.send(json.dumps({
                                            "type": "input_audio_buffer.append",
                                            "audio": audio_b64
                                        }))
                                    except websockets.exceptions.ConnectionClosed:
                                        asyncio.create_task(self._reconnect_openai())
                                        return
                                pre_buffer.clear()
                                print(f"[PHONE-BRIDGE] OpenAI VAD: Speech started ({speech_frames} frames)")
                        else:
                            speech_frames = 0
                    else:
                        # Currently speaking — send frame to OpenAI
                        try:
                            pcm_24k = AudioConverter.phone_to_ai(frame, 24000)
                            audio_b64 = base64.b64encode(pcm_24k).decode('ascii')
                            await self._ai_session.openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": audio_b64
                            }))
                        except websockets.exceptions.ConnectionClosed:
                            asyncio.create_task(self._reconnect_openai())
                            return

                        if is_speech:
                            speech_frames += 1
                            silence_frames = 0
                        else:
                            silence_frames += 1

                            if silence_frames >= VAD_SILENCE_FRAMES:
                                speech_duration_ms = speech_frames * VAD_FRAME_MS
                                print(f"[PHONE-BRIDGE] OpenAI VAD: Speech ended ({speech_duration_ms}ms)")

                                # No silence burst needed — during the 800ms silence detection
                                # period, natural phone-line silence was already being sent to
                                # OpenAI. Its server-side VAD sees that silence and commits.
                                # Sending pure digital zeros caused audible static/dropout.

                                # Reset state
                                is_speaking = False
                                speech_frames = 0
                                silence_frames = 0
                                pre_buffer.clear()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[PHONE-BRIDGE] OpenAI VAD error: {e}")
                await asyncio.sleep(0.1)

        print("[PHONE-BRIDGE] OpenAI VAD processor stopped")

    async def _openai_direct_send_fallback(self):
        """Fallback: send audio directly to OpenAI without VAD (if webrtcvad unavailable)."""
        if not hasattr(self, '_openai_vad_queue'):
            self._openai_vad_queue = asyncio.Queue()

        while self._running:
            try:
                ulaw_data = await asyncio.wait_for(
                    self._openai_vad_queue.get(), timeout=0.5
                )
                if self._ai_session and self._ai_session.openai_ws:
                    pcm_data = AudioConverter.phone_to_ai(ulaw_data, 24000)
                    audio_b64 = base64.b64encode(pcm_data).decode('ascii')
                    await self._ai_session.openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64
                    }))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[PHONE-BRIDGE] OpenAI direct send error: {e}")
                await asyncio.sleep(0.1)

    async def _openai_phone_listener(self):
        """Listen for OpenAI events and convert audio for phone."""
        from Orchestrator.routes.realtime_routes import (
            handle_openai_message,
            execute_search_snapshots,
        )
        from Orchestrator.web_tools import perform_web_search, perform_web_fetch
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType

        print("[PHONE-BRIDGE] OpenAI phone listener started")
        message_count = 0

        try:
            async for message in self._ai_session.openai_ws:
                message_count += 1
                # Update health monitor timestamp
                import time
                self._last_ai_message_time = time.time()

                if not self._running:
                    print(f"[PHONE-BRIDGE] OpenAI listener exiting: _running=False after {message_count} messages")
                    break

                event = json.loads(message)
                event_type = event.get("type", "")

                # Log all event types to debug
                if message_count <= 10 or message_count % 50 == 0:
                    print(f"[PHONE-BRIDGE] OpenAI event #{message_count}: {event_type}")

                # Handle audio - convert and send to phone
                if event_type == "response.audio.delta":
                    self._openai_is_speaking = True
                    audio_b64 = event.get("delta", "")
                    if audio_b64 and (self.on_ai_pcm16 or self.on_ai_audio):
                        try:
                            pcm_data = base64.b64decode(audio_b64)
                            if self.on_ai_pcm16:
                                # Direct PCM16 path (cellular — no ULAW)
                                await self.on_ai_pcm16(pcm_data, 24000)
                            else:
                                # Legacy ULAW path (Twilio)
                                ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)
                                await self.on_ai_audio(ulaw_data)
                        except Exception as audio_err:
                            print(f"[PHONE-BRIDGE] Error sending audio to phone: {audio_err}")
                            if "closed" in str(audio_err).lower():
                                self._running = False
                                break

                # Handle AI transcript
                elif event_type == "response.audio_transcript.delta":
                    delta = event.get("delta", "")
                    self._ai_session.transcript_buffer += delta

                elif event_type == "response.audio_transcript.done":
                    # AI finished speaking — update VAD mute state
                    import time as _time
                    self._openai_is_speaking = False
                    self._openai_speech_end_time = _time.time()

                    transcript = event.get("transcript", "") or self._ai_session.transcript_buffer
                    if transcript:
                        print(f"[PHONE-BRIDGE] AI: {transcript[:100]}...")
                        self._ai_session.conversation.append({
                            "role": "assistant",
                            "content": transcript,
                            "timestamp": now_utc_iso()
                        })
                        try:
                            if self.on_ai_transcript:
                                await self.on_ai_transcript(transcript)
                        except Exception as tx_err:
                            print(f"[PHONE-BRIDGE] Error sending transcript: {tx_err}")
                    self._ai_session.transcript_buffer = ""

                # Handle user transcript — filter Whisper hallucinations
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    from Orchestrator.whisper_filter import is_whisper_hallucination
                    transcript = event.get("transcript", "")
                    if transcript and transcript.strip() and not is_whisper_hallucination(transcript):
                        print(f"[PHONE-BRIDGE] User: {transcript}")
                        self._ai_session.conversation.append({
                            "role": "user",
                            "content": transcript,
                            "timestamp": now_utc_iso(),
                            "source": "voice"
                        })

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    call_id = event.get("call_id", "")
                    name = event.get("name", "")
                    arguments_str = event.get("arguments", "{}")

                    try:
                        arguments = json.loads(arguments_str)
                    except:
                        arguments = {}

                    print(f"[PHONE-BRIDGE] Tool call: {name}")
                    try:
                        result = await asyncio.wait_for(
                            self._execute_tool(name, arguments),
                            timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        print(f"[PHONE-BRIDGE] Tool '{name}' timed out after 30s")
                        result = f"Tool '{name}' timed out after 30 seconds"

                    # Send result back
                    await self._ai_session.openai_ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result
                        }
                    }))
                    await self._ai_session.openai_ws.send(json.dumps({
                        "type": "response.create"
                    }))

                elif event_type == "error":
                    error = event.get("error", {})
                    print(f"[PHONE-BRIDGE] OpenAI error: {error}")

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] OpenAI connection closed in listener: code={e.code}, reason={e.reason}, messages_received={message_count}")
            # Attempt reconnection instead of stopping
            if self._running and not self._openai_reconnecting:
                print("[PHONE-BRIDGE] Triggering reconnection from listener...")
                reconnected = await self._reconnect_openai()
                if reconnected:
                    return  # New listener started by reconnect
        except Exception as e:
            print(f"[PHONE-BRIDGE] OpenAI listener error: {type(e).__name__}: {e}, messages_received={message_count}")
            traceback.print_exc()
        finally:
            # Only stop if we're not reconnecting
            if not self._openai_reconnecting:
                self._running = False
            print(f"[PHONE-BRIDGE] OpenAI phone listener stopped after {message_count} messages")

    # =========================================================================
    # Gemini Live - Uses existing gemini_live_routes.py
    # =========================================================================

    async def _start_gemini(self) -> bool:
        """
        Start Gemini using the EXACT SAME message handler as Portal.

        REFACTORED (2026-02): Instead of a custom phone listener, we now use:
        1. PhoneWebSocketAdapter - makes phone bridge look like Portal WebSocket
        2. gemini_listener() from gemini_live_routes.py - the proven Portal handler

        This ensures phone calls have identical latency/behavior to Portal UI.
        """
        from Orchestrator.routes.gemini_live_routes import (
            connect_to_gemini,
            configure_gemini_session,
            gemini_listener,  # Use the SAME listener as Portal
        )
        from Orchestrator.config import GEMINI_LIVE_DEFAULT_VOICE

        # Create a GeminiLiveSession
        session_id = f"phone-{self.phone_session.session_id}"
        self._ai_session = GeminiLiveSession(
            session_id=session_id,
            operator=self.phone_session.operator,
            status="connecting",
        )
        GEMINI_LIVE_SESSIONS[session_id] = self._ai_session

        # Track reconnection state
        self._gemini_reconnecting = False
        self._gemini_reconnect_count = 0
        self._gemini_max_reconnects = 5

        # Connect using existing function
        success = await connect_to_gemini(self._ai_session)
        if not success:
            print(f"[PHONE-BRIDGE] Failed to connect to Gemini")
            return False

        # CRITICAL: Create adapter that routes Portal messages to phone
        # This makes the session look like a Portal connection to gemini_listener
        self._phone_adapter = PhoneWebSocketAdapter(self)
        self._ai_session.portal_ws = self._phone_adapter

        print(f"[PHONE-BRIDGE] Using Portal's gemini_listener via PhoneWebSocketAdapter")

        # Configure using existing function
        # Pass custom_role for outbound calls if provided
        # Pass phone_mode=True to enable server-side VAD with phone-tuned parameters
        custom_role = getattr(self.phone_session, 'outbound_role', '')
        await configure_gemini_session(
            self._ai_session,
            self.phone_session.operator,
            voice=GEMINI_LIVE_DEFAULT_VOICE,
            custom_role=custom_role,
            phone_mode=True  # Enable server VAD with phone-tuned sensitivity
        )

        # Start the SAME listener that Portal uses (via our adapter)
        task = asyncio.create_task(self._gemini_listener_with_reconnect())
        self._tasks.append(task)

        # NOTE: No client-side VAD for Gemini phone path.
        # Gemini's server-side automatic activity detection handles turn detection.
        # Audio streams directly — server detects speech start/end (matching OpenAI pattern).

        # For outbound calls with a greeting, trigger initial response
        is_outbound = getattr(self.phone_session, 'direction', 'inbound') == 'outbound'
        custom_greeting = getattr(self.phone_session, 'outbound_greeting', '')

        if is_outbound:
            await asyncio.sleep(0.5)
            asyncio.create_task(self._send_gemini_initial_greeting(custom_greeting))

        return True

    async def _gemini_listener_with_reconnect(self):
        """
        Wrapper around Portal's gemini_listener that handles reconnection for phone.

        The Portal doesn't need reconnection logic (browser handles it), but phone
        calls need to stay connected through brief network hiccups.
        """
        from Orchestrator.routes.gemini_live_routes import (
            gemini_listener,
            connect_to_gemini,
            configure_gemini_session,
        )
        from Orchestrator.config import GEMINI_LIVE_DEFAULT_VOICE

        print(f"[PHONE-BRIDGE] Starting Gemini listener (using Portal's handler)")

        while self._running and self._gemini_reconnect_count <= self._gemini_max_reconnects:
            try:
                # Run the Portal's gemini_listener
                await gemini_listener(self._ai_session)

                # If we get here, listener ended normally (connection closed)
                if not self._running:
                    break

                print(f"[PHONE-BRIDGE] Gemini listener ended, attempting reconnect...")

            except Exception as e:
                print(f"[PHONE-BRIDGE] Gemini listener error: {type(e).__name__}: {e}")
                if not self._running:
                    break

            # Attempt reconnection
            if self._running and not self._gemini_reconnecting:
                self._gemini_reconnecting = True
                self._gemini_reconnect_count += 1

                if self._gemini_reconnect_count > self._gemini_max_reconnects:
                    print(f"[PHONE-BRIDGE] Max Gemini reconnects ({self._gemini_max_reconnects}) reached")
                    break

                print(f"[PHONE-BRIDGE] Reconnecting to Gemini ({self._gemini_reconnect_count}/{self._gemini_max_reconnects})...")

                try:
                    # Close old connection
                    if self._ai_session.gemini_ws:
                        try:
                            await self._ai_session.gemini_ws.close()
                        except:
                            pass

                    await asyncio.sleep(1)  # Brief delay before reconnect

                    # Reconnect
                    success = await connect_to_gemini(self._ai_session)
                    if success:
                        # Reconfigure with phone_mode=True for explicit push-to-talk
                        custom_role = getattr(self.phone_session, 'outbound_role', '')
                        await configure_gemini_session(
                            self._ai_session,
                            self.phone_session.operator,
                            voice=GEMINI_LIVE_DEFAULT_VOICE,
                            custom_role=custom_role,
                            phone_mode=True
                        )
                        print(f"[PHONE-BRIDGE] Gemini reconnection successful")
                        self._gemini_reconnecting = False
                        # Loop will restart gemini_listener
                    else:
                        print(f"[PHONE-BRIDGE] Gemini reconnection failed")
                        self._gemini_reconnecting = False
                        break

                except Exception as reconnect_err:
                    print(f"[PHONE-BRIDGE] Gemini reconnection error: {reconnect_err}")
                    self._gemini_reconnecting = False
                    break

        if not self._gemini_reconnecting:
            self._running = False
        print(f"[PHONE-BRIDGE] Gemini listener with reconnect stopped")

    async def _send_gemini_initial_greeting(self, custom_greeting: str = ""):
        """Send an initial greeting to trigger Gemini response for outbound calls."""
        if not self._ai_session or not self._ai_session.gemini_ws:
            return

        try:
            if custom_greeting:
                prompt = f"The user just answered the phone. Greet them and deliver this message: {custom_greeting}"
            else:
                prompt = f"The user just answered the phone. Introduce yourself briefly and ask how you can help."

            print(f"[PHONE-BRIDGE] Sending Gemini initial greeting prompt: {prompt[:80]}...")

            # Send text message to trigger response
            await self._ai_session.gemini_ws.send(json.dumps({
                "clientContent": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": f"[SYSTEM: {prompt}]"}]
                    }],
                    "turnComplete": True
                }
            }))

            print(f"[PHONE-BRIDGE] Gemini initial greeting triggered")

        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to send Gemini initial greeting: {e}")

    async def _send_gemini_audio(self, ulaw_data: bytes):
        """Convert and send audio directly to Gemini (matching OpenAI pattern).

        Server-side automatic activity detection handles turn detection.
        No client-side VAD needed — Gemini detects speech boundaries natively.
        """
        if not self._ai_session or not self._ai_session.gemini_ws:
            return

        if self._gemini_reconnecting:
            return  # Don't send while reconnecting

        try:
            # Convert ULAW 8kHz to PCM16 16kHz (Gemini uses 16kHz input)
            pcm_data = AudioConverter.phone_to_ai(ulaw_data, 16000)
            audio_b64 = base64.b64encode(pcm_data).decode('ascii')

            await self._ai_session.gemini_ws.send(json.dumps({
                "realtimeInput": {
                    "mediaChunks": [{
                        "mimeType": "audio/pcm;rate=16000",
                        "data": audio_b64
                    }]
                }
            }))
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] Gemini connection closed while sending: code={e.code}")
            # Reconnection handled by _gemini_listener_with_reconnect

    async def _gemini_vad_processor(self):
        """Process audio for Gemini using WebRTC VAD.

        CRITICAL: Gemini detects end-of-speech by detecting pauses in audio.
        The Portal works because the browser only sends audio when capturing.
        Phone audio streams continuously, so we must gate it with VAD.

        Only sends audio to Gemini during detected speech, creating natural
        pauses that allow Gemini to respond.
        """
        import struct
        import time

        # Initialize WebRTC VAD
        if WEBRTCVAD_AVAILABLE:
            vad = webrtcvad.Vad()
            vad.set_mode(2)  # Mode 2: balanced (less aggressive than Grok's mode 3)
            print("[PHONE-BRIDGE] Gemini VAD processor started (WebRTC mode 2)")
        else:
            print("[PHONE-BRIDGE] WebRTC VAD not available - falling back to continuous send")
            await self._gemini_continuous_audio_fallback()
            return

        # VAD parameters - tuned for 8kHz modem audio (matched to Grok's proven values)
        VAD_FRAME_MS = 20
        VAD_SAMPLE_RATE = 8000
        VAD_FRAME_BYTES = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 320 bytes
        VAD_SILENCE_FRAMES = 75  # 1.5s of silence before activityEnd (prevents premature cutoff)
        VAD_MIN_SPEECH_FRAMES = 15  # 300ms minimum speech
        VAD_PRE_BUFFER_FRAMES = 25  # 500ms pre-buffer to catch speech onset
        VAD_GRACE_PERIOD = 1.0  # 1s grace period after Gemini finishes speaking
        VAD_ENERGY_THRESHOLD = 500  # RMS energy threshold (matched to Grok)

        # State
        is_speaking = False
        speech_frames = 0
        silence_frames = 0
        pre_buffer = []
        frame_buffer = bytearray()
        audio_buffer_16k = bytearray()  # Accumulate speech for sending

        # Initialize Gemini speaking state
        if not hasattr(self, '_gemini_is_speaking'):
            self._gemini_is_speaking = False
        if not hasattr(self, '_gemini_speech_end_time'):
            self._gemini_speech_end_time = None

        # Create queue if not exists
        if not hasattr(self, '_gemini_vad_queue'):
            self._gemini_vad_queue = asyncio.Queue()

        while self._running:
            try:
                # Get audio with timeout
                try:
                    ulaw_data = await asyncio.wait_for(
                        self._gemini_vad_queue.get(),
                        timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                # Convert ULAW to PCM16 at 8kHz (for VAD analysis)
                pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ulaw_data)
                frame_buffer.extend(pcm_8k)

                # Check if VAD should be muted (Gemini is speaking or in grace period)
                vad_muted = False
                if self._gemini_is_speaking:
                    vad_muted = True
                elif self._gemini_speech_end_time:
                    elapsed = time.time() - self._gemini_speech_end_time
                    if elapsed < VAD_GRACE_PERIOD:
                        vad_muted = True
                    else:
                        self._gemini_speech_end_time = None

                if vad_muted:
                    frame_buffer.clear()
                    audio_buffer_16k.clear()
                    if is_speaking:
                        is_speaking = False
                        speech_frames = 0
                        silence_frames = 0
                        pre_buffer.clear()
                    continue

                # Process complete VAD frames
                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                    frame_buffer = frame_buffer[VAD_FRAME_BYTES:]

                    # Calculate RMS energy
                    samples = struct.unpack(f"<{len(frame)//2}h", frame)
                    rms_energy = (sum(s*s for s in samples) / len(samples)) ** 0.5

                    # Determine if this frame contains speech
                    if rms_energy < VAD_ENERGY_THRESHOLD:
                        is_speech = False
                    else:
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
                            # Speech started - add pre-buffer
                            is_speaking = True
                            speech_frames = 1
                            silence_frames = 0

                            # Send activityStart to tell Gemini user is about to speak
                            try:
                                await self._ai_session.gemini_ws.send(json.dumps({
                                    "realtimeInput": {
                                        "activityStart": {}
                                    }
                                }))
                            except:
                                pass

                            for pf in pre_buffer:
                                pcm_16k = AudioConverter.upsample(pf, 2)
                                audio_buffer_16k.extend(pcm_16k)
                            pre_buffer.clear()
                            # Add current frame
                            pcm_16k = AudioConverter.upsample(frame, 2)
                            audio_buffer_16k.extend(pcm_16k)
                            print(f"[PHONE-BRIDGE] Gemini VAD: Speech started (sent activityStart)")
                    else:
                        # Currently speaking - add frame to buffer
                        pcm_16k = AudioConverter.upsample(frame, 2)
                        audio_buffer_16k.extend(pcm_16k)

                        if is_speech:
                            speech_frames += 1
                            silence_frames = 0
                        else:
                            silence_frames += 1

                            if silence_frames >= VAD_SILENCE_FRAMES:
                                # End of speech detected - send accumulated audio then STOP
                                speech_duration_ms = speech_frames * VAD_FRAME_MS
                                print(f"[PHONE-BRIDGE] Gemini VAD: Speech ended ({speech_duration_ms}ms)")

                                if speech_frames >= VAD_MIN_SPEECH_FRAMES:
                                    # Send accumulated audio to Gemini
                                    try:
                                        audio_b64 = base64.b64encode(bytes(audio_buffer_16k)).decode('ascii')
                                        await self._ai_session.gemini_ws.send(json.dumps({
                                            "realtimeInput": {
                                                "mediaChunks": [{
                                                    "mimeType": "audio/pcm;rate=16000",
                                                    "data": audio_b64
                                                }]
                                            }
                                        }))
                                        # Buffer for Whisper
                                        self._ai_session.user_audio_buffer.append(audio_b64)

                                        # CRITICAL: Send activityEnd to tell Gemini the user is done speaking
                                        # Without this, Gemini waits for more audio and may not respond
                                        await self._ai_session.gemini_ws.send(json.dumps({
                                            "realtimeInput": {
                                                "activityEnd": {}
                                            }
                                        }))
                                        print(f"[PHONE-BRIDGE] Gemini VAD: Sent {len(audio_buffer_16k)} bytes + activityEnd, waiting for response...")
                                    except websockets.exceptions.ConnectionClosed as e:
                                        print(f"[PHONE-BRIDGE] Gemini connection closed: {e.code}")
                                        if self._running and not self._gemini_reconnecting:
                                            pass  # Reconnect handled by listener
                                else:
                                    print(f"[PHONE-BRIDGE] Gemini VAD: Discarded ({speech_duration_ms}ms too short)")

                                # Reset - STOP SENDING until next speech
                                audio_buffer_16k.clear()
                                is_speaking = False
                                speech_frames = 0
                                silence_frames = 0

                        # Send audio chunks periodically during speech (every 500ms)
                        if is_speaking and len(audio_buffer_16k) >= 16000:  # 500ms at 16kHz
                            try:
                                audio_b64 = base64.b64encode(bytes(audio_buffer_16k)).decode('ascii')
                                await self._ai_session.gemini_ws.send(json.dumps({
                                    "realtimeInput": {
                                        "mediaChunks": [{
                                            "mimeType": "audio/pcm;rate=16000",
                                            "data": audio_b64
                                        }]
                                    }
                                }))
                                self._ai_session.user_audio_buffer.append(audio_b64)
                                audio_buffer_16k.clear()
                            except websockets.exceptions.ConnectionClosed:
                                pass

            except Exception as e:
                print(f"[PHONE-BRIDGE] Gemini VAD error: {e}")
                await asyncio.sleep(0.1)

    async def _gemini_continuous_audio_fallback(self):
        """Fallback: send audio continuously if VAD unavailable."""
        print("[PHONE-BRIDGE] WARNING: Running Gemini without VAD - turn detection may fail")
        if not hasattr(self, '_gemini_vad_queue'):
            self._gemini_vad_queue = asyncio.Queue()

        while self._running:
            try:
                ulaw_data = await asyncio.wait_for(
                    self._gemini_vad_queue.get(),
                    timeout=0.5
                )
                pcm_data = AudioConverter.phone_to_ai(ulaw_data, 16000)
                audio_b64 = base64.b64encode(pcm_data).decode('ascii')
                await self._ai_session.gemini_ws.send(json.dumps({
                    "realtimeInput": {
                        "mediaChunks": [{
                            "mimeType": "audio/pcm;rate=16000",
                            "data": audio_b64
                        }]
                    }
                }))
                self._ai_session.user_audio_buffer.append(audio_b64)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[PHONE-BRIDGE] Gemini fallback error: {e}")
                await asyncio.sleep(0.1)

    async def _reconnect_gemini(self) -> bool:
        """
        DEPRECATED: Reconnection is now handled by _gemini_listener_with_reconnect().
        This method is kept for backwards compatibility but should not be called.
        """
        print(f"[PHONE-BRIDGE] WARNING: _reconnect_gemini() is deprecated, using _gemini_listener_with_reconnect() instead")
        return False

    async def _gemini_phone_listener_DEPRECATED(self):
        """
        DEPRECATED (2026-02): Custom phone listener replaced by Portal's gemini_listener.

        This method is kept for reference but should NOT be called.
        The new approach uses:
        - PhoneWebSocketAdapter: Routes Portal messages to phone audio output
        - gemini_listener(): The proven Portal message handler
        - _gemini_listener_with_reconnect(): Wrapper with phone-specific reconnection

        See _start_gemini() for the new implementation.
        """
        raise DeprecationWarning("Use _gemini_listener_with_reconnect() instead")

        # OLD CODE BELOW - kept for reference
        from Orchestrator.routes.gemini_live_routes import (
            transcribe_user_audio,
            execute_search_snapshots,
        )
        from Orchestrator.web_tools import perform_web_search, perform_web_fetch
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType

        print("[PHONE-BRIDGE] Gemini phone listener started")
        is_speaking = False
        message_count = 0

        try:
            async for message in self._ai_session.gemini_ws:
                message_count += 1
                if not self._running:
                    print(f"[PHONE-BRIDGE] Gemini listener exiting: _running=False after {message_count} messages")
                    break

                event = json.loads(message)

                # Setup complete
                if "setupComplete" in event:
                    print("[PHONE-BRIDGE] Gemini setup complete")
                    continue

                # Server content (audio/text)
                if "serverContent" in event:
                    content = event["serverContent"]
                    turn_complete = content.get("turnComplete", False)

                    if "modelTurn" in content:
                        for part in content["modelTurn"].get("parts", []):
                            # Audio
                            if "inlineData" in part:
                                inline_data = part["inlineData"]
                                if "audio/pcm" in inline_data.get("mimeType", ""):
                                    audio_b64 = inline_data.get("data", "")
                                    if audio_b64 and self.on_ai_audio:
                                        # Transcribe user audio when AI starts speaking (non-blocking)
                                        if not is_speaking and self._ai_session.user_audio_buffer:
                                            # Run transcription in background - don't block audio playback
                                            async def transcribe_background():
                                                try:
                                                    transcript = await transcribe_user_audio(self._ai_session)
                                                    if transcript:
                                                        print(f"[PHONE-BRIDGE] User: {transcript}")
                                                        self._ai_session.conversation.append({
                                                            "role": "user",
                                                            "content": transcript,
                                                            "timestamp": now_utc_iso(),
                                                            "source": "voice"
                                                        })
                                                except Exception as e:
                                                    print(f"[PHONE-BRIDGE] Transcription error: {e}")
                                            asyncio.create_task(transcribe_background())

                                        is_speaking = True
                                        try:
                                            pcm_data = base64.b64decode(audio_b64)
                                            ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)
                                            await self.on_ai_audio(ulaw_data)
                                        except Exception as audio_err:
                                            print(f"[PHONE-BRIDGE] Error sending Gemini audio to phone: {audio_err}")
                                            if "closed" in str(audio_err).lower():
                                                self._running = False

                            # Text transcript
                            if "text" in part:
                                self._ai_session.transcript_buffer += part["text"]

                    if turn_complete:
                        is_speaking = False
                        if self._ai_session.transcript_buffer:
                            print(f"[PHONE-BRIDGE] AI: {self._ai_session.transcript_buffer[:100]}...")
                            self._ai_session.conversation.append({
                                "role": "assistant",
                                "content": self._ai_session.transcript_buffer,
                                "timestamp": now_utc_iso()
                            })
                            try:
                                if self.on_ai_transcript:
                                    await self.on_ai_transcript(self._ai_session.transcript_buffer)
                            except Exception as tx_err:
                                print(f"[PHONE-BRIDGE] Error sending Gemini transcript: {tx_err}")
                            self._ai_session.transcript_buffer = ""

                # Tool calls
                if "toolCall" in event:
                    tool_call = event["toolCall"]
                    for fc in tool_call.get("functionCalls", []):
                        call_id = fc.get("id", "")
                        name = fc.get("name", "")
                        args = fc.get("args", {})

                        print(f"[PHONE-BRIDGE] Gemini tool call: {name}")
                        try:
                            result = await asyncio.wait_for(
                                self._execute_tool(name, args),
                                timeout=30.0
                            )
                        except asyncio.TimeoutError:
                            print(f"[PHONE-BRIDGE] Gemini tool '{name}' timed out after 30s")
                            result = f"Tool '{name}' timed out after 30 seconds"

                        await self._ai_session.gemini_ws.send(json.dumps({
                            "toolResponse": {
                                "functionResponses": [{
                                    "id": call_id,
                                    "name": name,
                                    "response": {"result": result}
                                }]
                            }
                        }))

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] Gemini connection closed in listener: code={e.code}, reason={e.reason}, messages_received={message_count}")
            # Attempt reconnection instead of stopping
            if self._running and not self._gemini_reconnecting:
                print("[PHONE-BRIDGE] Triggering Gemini reconnection from listener...")
                reconnected = await self._reconnect_gemini()
                if reconnected:
                    return  # New listener started by reconnect
        except Exception as e:
            print(f"[PHONE-BRIDGE] Gemini listener error: {type(e).__name__}: {e}, messages_received={message_count}")
            traceback.print_exc()
            # Also try reconnection on other errors
            if self._running and not self._gemini_reconnecting:
                reconnected = await self._reconnect_gemini()
                if reconnected:
                    return
        finally:
            # Only stop if we're not reconnecting
            if not self._gemini_reconnecting:
                self._running = False
            print(f"[PHONE-BRIDGE] Gemini phone listener stopped after {message_count} messages")

    # =========================================================================
    # Grok Live - Uses existing grok_live_routes.py
    # =========================================================================

    async def _start_grok(self) -> bool:
        """Start Grok using existing route handlers."""
        from Orchestrator.routes.grok_live_routes import (
            connect_to_grok,
            configure_grok_session,
        )
        from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE

        # Create a GrokLiveSession
        session_id = f"phone-{self.phone_session.session_id}"
        self._ai_session = GrokLiveSession(
            session_id=session_id,
            operator=self.phone_session.operator,
            status="connecting",
        )
        GROK_LIVE_SESSIONS[session_id] = self._ai_session

        # Track reconnection state
        self._grok_reconnecting = False
        self._grok_reconnect_count = 0
        self._grok_max_reconnects = 5

        # Connect using existing function
        success = await connect_to_grok(self._ai_session)
        if not success:
            return False

        # Configure using existing function
        # Pass custom_role for outbound calls if provided
        custom_role = getattr(self.phone_session, 'outbound_role', '')
        await configure_grok_session(
            self._ai_session,
            self.phone_session.operator,
            voice=GROK_LIVE_DEFAULT_VOICE,
            custom_role=custom_role
        )

        # PHONE: Re-enable Grok's server-side VAD (matching OpenAI pattern)
        # Server-side VAD handles turn detection — no client-side VAD needed.
        # Tuned for phone audio: higher threshold (0.8) for noise, 1000ms silence.
        print("[PHONE-BRIDGE] Enabling Grok server-side VAD for phone (matching OpenAI pattern)")
        await self._ai_session.grok_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.8,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 1000
                }
            }
        }))

        # Start listener
        task = asyncio.create_task(self._grok_phone_listener())
        self._tasks.append(task)

        # NOTE: No client-side VAD for Grok phone path.
        # Grok's server-side VAD handles turn detection (matching OpenAI pattern).
        # Audio streams directly — server detects speech start/end.

        # For outbound calls with a greeting, trigger initial response
        is_outbound = getattr(self.phone_session, 'direction', 'inbound') == 'outbound'
        custom_greeting = getattr(self.phone_session, 'outbound_greeting', '')

        if is_outbound:
            await asyncio.sleep(0.5)
            asyncio.create_task(self._send_grok_initial_greeting(custom_greeting))

        return True

    async def _send_grok_initial_greeting(self, custom_greeting: str = ""):
        """Send an initial greeting to trigger Grok response for outbound calls."""
        if not self._ai_session or not self._ai_session.grok_ws:
            return

        try:
            if custom_greeting:
                prompt = f"The user just answered the phone. Greet them and deliver this message: {custom_greeting}"
            else:
                prompt = f"The user just answered the phone. Introduce yourself briefly and ask how you can help."

            print(f"[PHONE-BRIDGE] Sending Grok initial greeting prompt: {prompt[:80]}...")

            # Send as a conversation item (similar to OpenAI format)
            await self._ai_session.grok_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": f"[SYSTEM: {prompt}]"
                    }]
                }
            }))

            # Request response
            await self._ai_session.grok_ws.send(json.dumps({
                "type": "response.create"
            }))

            print(f"[PHONE-BRIDGE] Grok initial greeting triggered")

        except Exception as e:
            print(f"[PHONE-BRIDGE] Failed to send Grok initial greeting: {e}")

    async def _reconnect_grok(self) -> bool:
        """Attempt to reconnect to Grok after connection drop."""
        from Orchestrator.routes.grok_live_routes import (
            connect_to_grok,
            configure_grok_session,
        )
        from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE

        if self._grok_reconnecting:
            return False  # Already reconnecting

        self._grok_reconnect_count += 1
        if self._grok_reconnect_count > self._grok_max_reconnects:
            print(f"[PHONE-BRIDGE] Max Grok reconnection attempts ({self._grok_max_reconnects}) reached, giving up")
            return False

        self._grok_reconnecting = True
        print(f"[PHONE-BRIDGE] Attempting Grok reconnection ({self._grok_reconnect_count}/{self._grok_max_reconnects})...")

        try:
            # Close old connection if exists
            if self._ai_session and self._ai_session.grok_ws:
                try:
                    await self._ai_session.grok_ws.close()
                except:
                    pass

            # Brief delay before reconnect
            await asyncio.sleep(1)

            # Reconnect
            self._ai_session.status = "reconnecting"
            success = await connect_to_grok(self._ai_session)
            if not success:
                print("[PHONE-BRIDGE] Grok reconnection failed - could not connect")
                self._grok_reconnecting = False
                return False

            # Reconfigure session
            custom_role = getattr(self.phone_session, 'outbound_role', '')
            await configure_grok_session(
                self._ai_session,
                self.phone_session.operator,
                voice=GROK_LIVE_DEFAULT_VOICE,
                custom_role=custom_role
            )

            # Start new listener
            task = asyncio.create_task(self._grok_phone_listener())
            self._tasks.append(task)

            print(f"[PHONE-BRIDGE] Grok reconnected successfully!")
            self._grok_reconnecting = False
            return True

        except Exception as e:
            print(f"[PHONE-BRIDGE] Grok reconnection error: {e}")
            self._grok_reconnecting = False
            return False

    async def _send_grok_audio(self, ulaw_data: bytes):
        """Convert and send audio directly to Grok (matching OpenAI pattern).

        Server-side VAD handles turn detection. No client-side VAD needed.
        """
        if not self._ai_session or not self._ai_session.grok_ws:
            return

        if self._grok_reconnecting:
            return  # Don't send while reconnecting

        try:
            # Convert ULAW 8kHz to PCM16 24kHz
            pcm_data = AudioConverter.phone_to_ai(ulaw_data, 24000)
            audio_b64 = base64.b64encode(pcm_data).decode('ascii')

            await self._ai_session.grok_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": audio_b64
            }))
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] Grok connection closed while sending: code={e.code}, reason={e.reason}")
            asyncio.create_task(self._reconnect_grok())

    async def _grok_vad_processor(self):
        """Process buffered audio for Grok using WebRTC VAD."""
        import struct
        import time

        # Initialize WebRTC VAD
        if WEBRTCVAD_AVAILABLE:
            vad = webrtcvad.Vad()
            vad.set_mode(3)  # Most aggressive filtering for noisy phone
            print("[PHONE-BRIDGE] Grok VAD processor started (WebRTC mode 3)")
        else:
            print("[PHONE-BRIDGE] WebRTC VAD not available - Grok voice disabled")
            return

        # VAD parameters
        VAD_FRAME_MS = 20
        VAD_SAMPLE_RATE = 8000
        VAD_FRAME_BYTES = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 320 bytes
        VAD_SILENCE_FRAMES = 75  # 1.5 seconds of silence to end utterance
        VAD_MIN_SPEECH_FRAMES = 15  # 300ms minimum
        VAD_PRE_BUFFER_FRAMES = 25  # 500ms pre-buffer
        VAD_GRACE_PERIOD = 1.0  # 1 second grace period after Grok finishes speaking
        VAD_ENERGY_THRESHOLD = 500  # Minimum RMS energy to consider as potential speech

        # State
        is_speaking = False
        speech_frames = 0
        silence_frames = 0
        pre_buffer = []
        frame_buffer = bytearray()

        # Initialize Grok speaking state flags
        if not hasattr(self, '_grok_is_speaking'):
            self._grok_is_speaking = False
        if not hasattr(self, '_grok_speech_end_time'):
            self._grok_speech_end_time = None

        # Create queue if not exists
        if not hasattr(self, '_grok_vad_queue'):
            self._grok_vad_queue = asyncio.Queue()

        while self._running:
            try:
                # Get audio with timeout
                try:
                    ulaw_data = await asyncio.wait_for(
                        self._grok_vad_queue.get(),
                        timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                # Convert ULAW to PCM16 at 8kHz (for VAD)
                pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ulaw_data)
                frame_buffer.extend(pcm_8k)

                # Also send audio to Grok continuously (it buffers internally)
                try:
                    pcm_24k = AudioConverter.phone_to_ai(ulaw_data, 24000)
                    audio_b64 = base64.b64encode(pcm_24k).decode('ascii')
                    await self._ai_session.grok_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64
                    }))
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"[PHONE-BRIDGE] Grok connection closed while sending: code={e.code}, reason={e.reason}")
                    asyncio.create_task(self._reconnect_grok())
                    continue

                # Check if VAD should be muted (Grok is speaking or in grace period)
                vad_muted = False
                if self._grok_is_speaking:
                    vad_muted = True
                elif self._grok_speech_end_time:
                    elapsed = time.time() - self._grok_speech_end_time
                    if elapsed < VAD_GRACE_PERIOD:
                        vad_muted = True
                    else:
                        self._grok_speech_end_time = None  # Grace period over

                if vad_muted:
                    # Clear frame buffer but don't process VAD
                    frame_buffer.clear()
                    # Reset speech state if we were in the middle of detecting
                    if is_speaking:
                        is_speaking = False
                        speech_frames = 0
                        silence_frames = 0
                        pre_buffer.clear()
                    continue

                # Process complete VAD frames
                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                    frame_buffer = frame_buffer[VAD_FRAME_BYTES:]

                    # Calculate RMS energy of the frame
                    samples = struct.unpack(f"<{len(frame)//2}h", frame)
                    rms_energy = (sum(s*s for s in samples) / len(samples)) ** 0.5

                    # Skip if energy is too low (floor noise)
                    if rms_energy < VAD_ENERGY_THRESHOLD:
                        is_speech = False
                    else:
                        # Run VAD only if energy threshold met
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
                            is_speaking = True
                            speech_frames = 1
                            silence_frames = 0
                            pre_buffer.clear()
                            print(f"[PHONE-BRIDGE] Grok VAD: Speech started")
                    else:
                        if is_speech:
                            speech_frames += 1
                            silence_frames = 0
                        else:
                            silence_frames += 1

                            if silence_frames >= VAD_SILENCE_FRAMES:
                                speech_duration_ms = speech_frames * VAD_FRAME_MS

                                if speech_frames >= VAD_MIN_SPEECH_FRAMES:
                                    print(f"[PHONE-BRIDGE] Grok VAD: Speech ended ({speech_duration_ms}ms), committing buffer")
                                    # Commit buffer and request response
                                    try:
                                        await self._ai_session.grok_ws.send(json.dumps({
                                            "type": "input_audio_buffer.commit"
                                        }))
                                        await self._ai_session.grok_ws.send(json.dumps({
                                            "type": "response.create"
                                        }))
                                    except Exception as e:
                                        print(f"[PHONE-BRIDGE] Grok commit error: {e}")
                                else:
                                    print(f"[PHONE-BRIDGE] Grok VAD: Discarded ({speech_duration_ms}ms too short)")

                                # Reset
                                is_speaking = False
                                speech_frames = 0
                                silence_frames = 0

            except Exception as e:
                print(f"[PHONE-BRIDGE] Grok VAD error: {e}")
                await asyncio.sleep(0.1)

        print("[PHONE-BRIDGE] Grok VAD processor stopped")

    async def _grok_phone_listener(self):
        """Listen for Grok events and convert audio for phone."""
        print("[PHONE-BRIDGE] Grok phone listener started")
        message_count = 0

        try:
            async for message in self._ai_session.grok_ws:
                message_count += 1
                # Update health monitor timestamp
                import time
                self._last_ai_message_time = time.time()

                if not self._running:
                    print(f"[PHONE-BRIDGE] Grok listener exiting: _running=False after {message_count} messages")
                    break

                event = json.loads(message)
                event_type = event.get("type", "")

                # Log all event types to debug (show all non-ping events)
                if event_type != "ping":
                    print(f"[PHONE-BRIDGE] Grok event #{message_count}: {event_type}")

                # Audio - Grok uses response.output_audio.delta (different from OpenAI)
                if event_type == "response.output_audio.delta":
                    # Mark that Grok is speaking (mutes VAD)
                    self._grok_is_speaking = True
                    self._grok_speech_end_time = None

                    audio_b64 = event.get("delta", "")
                    if audio_b64 and (self.on_ai_pcm16 or self.on_ai_audio):
                        try:
                            pcm_data = base64.b64decode(audio_b64)
                            if self.on_ai_pcm16:
                                # Direct PCM16 path (cellular — no ULAW)
                                await self.on_ai_pcm16(pcm_data, 24000)
                            else:
                                # Legacy ULAW path (Twilio)
                                ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)
                                await self.on_ai_audio(ulaw_data)
                        except Exception as audio_err:
                            print(f"[PHONE-BRIDGE] Error sending Grok audio to phone: {audio_err}")
                            if "closed" in str(audio_err).lower():
                                self._running = False
                                break

                # Detect when Grok stops speaking
                elif event_type == "response.output_audio.done":
                    import time
                    self._grok_is_speaking = False
                    self._grok_speech_end_time = time.time()  # Start grace period
                    print(f"[PHONE-BRIDGE] Grok audio done, starting VAD grace period")

                # AI transcript - Grok uses response.output_audio_transcript.delta
                elif event_type == "response.output_audio_transcript.delta":
                    self._ai_session.transcript_buffer += event.get("delta", "")

                elif event_type == "response.output_audio_transcript.done":
                    transcript = event.get("transcript", "") or self._ai_session.transcript_buffer
                    if transcript:
                        print(f"[PHONE-BRIDGE] AI: {transcript[:100]}...")
                        self._ai_session.conversation.append({
                            "role": "assistant",
                            "content": transcript,
                            "timestamp": now_utc_iso()
                        })
                        try:
                            if self.on_ai_transcript:
                                await self.on_ai_transcript(transcript)
                        except Exception as tx_err:
                            print(f"[PHONE-BRIDGE] Error sending Grok transcript: {tx_err}")
                    self._ai_session.transcript_buffer = ""

                # User transcript
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript", "")
                    if transcript and transcript.strip():
                        print(f"[PHONE-BRIDGE] User: {transcript}")
                        self._ai_session.conversation.append({
                            "role": "user",
                            "content": transcript,
                            "timestamp": now_utc_iso(),
                            "source": "voice"
                        })

                # Audio buffer committed - need to request a response from Grok
                elif event_type == "input_audio_buffer.committed":
                    print(f"[PHONE-BRIDGE] Grok audio committed, requesting response...")
                    try:
                        await self._ai_session.grok_ws.send(json.dumps({
                            "type": "response.create"
                        }))
                    except Exception as e:
                        print(f"[PHONE-BRIDGE] Error requesting Grok response: {e}")

                # Tool calls
                elif event_type == "response.function_call_arguments.done":
                    call_id = event.get("call_id", "")
                    name = event.get("name", "")
                    arguments_str = event.get("arguments", "{}")

                    try:
                        arguments = json.loads(arguments_str)
                    except:
                        arguments = {}

                    print(f"[PHONE-BRIDGE] Grok tool call: {name} (call_id={call_id})")
                    try:
                        result = await asyncio.wait_for(
                            self._execute_tool(name, arguments),
                            timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        print(f"[PHONE-BRIDGE] Grok tool '{name}' timed out after 30s")
                        result = f"Tool '{name}' timed out after 30 seconds"
                    print(f"[PHONE-BRIDGE] Grok tool result: {str(result)[:200]}...")

                    # Check WebSocket state before sending
                    if not self._ai_session.grok_ws:
                        print(f"[PHONE-BRIDGE] ERROR: Grok WebSocket is None, cannot send tool result!")
                        continue

                    try:
                        await self._ai_session.grok_ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": result
                            }
                        }))
                        print(f"[PHONE-BRIDGE] Grok tool output sent for {name}")

                        await self._ai_session.grok_ws.send(json.dumps({
                            "type": "response.create"
                        }))
                        print(f"[PHONE-BRIDGE] Grok response.create sent after tool {name}, waiting for audio...")
                    except Exception as send_err:
                        print(f"[PHONE-BRIDGE] ERROR sending Grok tool result: {send_err}")

                elif event_type == "response.done":
                    # Response completed - Grok has finished generating
                    response = event.get("response", {})
                    status = response.get("status", "unknown")
                    print(f"[PHONE-BRIDGE] Grok response.done: status={status}")
                    if status == "failed":
                        print(f"[PHONE-BRIDGE] Grok response failed: {response}")

                elif event_type == "error":
                    error = event.get("error", {})
                    print(f"[PHONE-BRIDGE] Grok error: {error}")

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[PHONE-BRIDGE] Grok connection closed in listener: code={e.code}, reason={e.reason}, messages_received={message_count}")
            # Attempt reconnection instead of stopping
            if self._running and not self._grok_reconnecting:
                print("[PHONE-BRIDGE] Triggering Grok reconnection from listener...")
                reconnected = await self._reconnect_grok()
                if reconnected:
                    return  # New listener started by reconnect
        except Exception as e:
            print(f"[PHONE-BRIDGE] Grok listener error: {type(e).__name__}: {e}, messages_received={message_count}")
            traceback.print_exc()
        finally:
            # Only stop if we're not reconnecting
            if not self._grok_reconnecting:
                self._running = False
            print(f"[PHONE-BRIDGE] Grok phone listener stopped after {message_count} messages")

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
                "working_directory": "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc",
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
- Media generation: generate_image, generate_video, generate_music
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
        """Initiate callback via /twilio/call or /cellular/call endpoint with session resumption."""
        if not AIOHTTP_AVAILABLE:
            return None

        # Determine call endpoint based on TELEPHONY_PROVIDER
        try:
            from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED
            if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
                call_url = "http://localhost:9091/asterisk/call"
            elif TELEPHONY_PROVIDER in ("cellular", "auto") and CELLULAR_ENABLED:
                call_url = "http://localhost:9091/cellular/call"
            else:
                call_url = "http://localhost:9091/twilio/call"
        except ImportError:
            call_url = "http://localhost:9091/twilio/call"

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
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error = await resp.text()
                        print(f"[PHONE-BRIDGE] Callback API error: {resp.status} - {error}")
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
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_STT_URL

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
                form.add_field('model', 'whisper-1')

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
        """Get response from Claude Code CLI session with full MCP tool access."""
        from Orchestrator.routes.agent_routes import create_claude_streaming
        from Orchestrator.state import PERSISTED_AGENT_SESSIONS, save_operator_state
        from Orchestrator.volume import now_utc_iso
        import select

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

            # Create Claude CLI process with appropriate flags
            working_dir = str(blackbox_root())
            process = create_claude_streaming(
                working_dir=working_dir,
                prompt=prompt,
                # Pass session_id for new sessions OR resume (to identify which session to resume)
                claude_session_id=self._claude_session_id if (is_new_session or is_resume) else None,
                continue_session=is_continuation,
                resume_session=is_resume,
                model="sonnet",  # Use sonnet for faster responses
                skip_permissions=True
            )
            self._claude_process = process

            # After resume, switch to continuation mode for subsequent messages
            if is_resume:
                self._resume_from_disk = False

            # Update persisted session
            if operator in PERSISTED_AGENT_SESSIONS:
                PERSISTED_AGENT_SESSIONS[operator]["message_count"] = self._claude_message_count
                PERSISTED_AGENT_SESSIONS[operator]["last_activity"] = now_utc_iso()
                save_operator_state()

            print(f"[PHONE-BRIDGE] Claude CLI started (msg #{self._claude_message_count}, new={is_new_session}, resume={is_resume}, continue={is_continuation})")

            # Read streaming JSON output
            response_text = ""
            loop = asyncio.get_event_loop()

            # Set non-blocking
            import fcntl
            import os
            fd = process.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

            # Read with timeout
            start_time = asyncio.get_event_loop().time()
            timeout = 120  # 2 minute max
            last_status_log = start_time
            tool_calls_seen = 0

            while process.poll() is None or process.stdout.readable():
                current_time = asyncio.get_event_loop().time()
                elapsed = current_time - start_time

                # Check timeout
                if elapsed > timeout:
                    print("[PHONE-BRIDGE] Claude CLI timeout")
                    process.terminate()
                    break

                # Periodic status logging (every 10 seconds) when phone disconnected
                if self._phone_disconnected and (current_time - last_status_log) >= 10:
                    last_status_log = current_time
                    print(f"[BACKGROUND] Claude still working... ({elapsed:.0f}s elapsed, {len(response_text)} chars collected, {tool_calls_seen} tool calls)")

                # Try to read
                try:
                    ready, _, _ = select.select([process.stdout], [], [], 0.1)
                    if ready:
                        line = process.stdout.readline()
                        if not line:
                            if process.poll() is not None:
                                break
                            continue

                        line = line.decode('utf-8', errors='replace').strip()
                        if not line:
                            continue

                        # Parse JSON
                        try:
                            event = json.loads(line)
                            event_type = event.get("type", "")

                            if event_type == "assistant":
                                # Collect text content
                                message = event.get("message", {})
                                content = message.get("content", [])
                                for block in content:
                                    if block.get("type") == "text":
                                        response_text += block.get("text", "")

                            elif event_type == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    response_text += delta.get("text", "")

                            elif event_type == "tool_use":
                                # Track tool calls for logging
                                tool_calls_seen += 1
                                tool_name = event.get("name", "unknown")
                                if self._phone_disconnected:
                                    print(f"[BACKGROUND] Claude using tool: {tool_name}")

                            elif event_type == "result":
                                # Final result
                                result = event.get("result", "")
                                if result and not response_text:
                                    response_text = result
                                break

                        except json.JSONDecodeError:
                            continue
                    else:
                        # No data ready, yield control
                        await asyncio.sleep(0.05)

                except Exception as e:
                    if process.poll() is not None:
                        break
                    await asyncio.sleep(0.05)

            # Clean up
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except:
                    process.kill()

            print(f"[PHONE-BRIDGE] Claude responded: {response_text[:100]}...")
            return response_text.strip() if response_text else None

        except Exception as e:
            print(f"[PHONE-BRIDGE] Claude CLI error: {e}")
            import traceback
            traceback.print_exc()
        return None

    async def _check_for_phone_actions(self, response_text: str):
        """Check if Claude's response indicates a phone action request."""
        # This is a simple pattern match - in the future we can use proper tool calling
        import re

        # Look for phone call patterns like "calling +1234567890" or "I'll call +1234567890"
        phone_pattern = r"(?:calling|call|dial(?:ing)?)\s*(\+?1?\d{10,})"
        match = re.search(phone_pattern, response_text.lower())

        if match:
            phone_number = match.group(1)
            # Normalize to E.164
            if not phone_number.startswith('+'):
                phone_number = '+1' + phone_number.lstrip('1')
            print(f"[PHONE-BRIDGE] Claude requested phone call to: {phone_number}")

            # Actually initiate the call via /twilio/call endpoint
            try:
                await self._initiate_outbound_call(phone_number)
            except Exception as e:
                print(f"[PHONE-BRIDGE] Failed to initiate call: {e}")

    async def _initiate_outbound_call(self, phone_number: str, greeting: str = "") -> Optional[dict]:
        """Initiate an outbound phone call via the /twilio/call endpoint."""
        if not AIOHTTP_AVAILABLE:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://localhost:9091/twilio/call",
                    json={
                        "to": phone_number,
                        "backend": "openai_realtime",  # Default to OpenAI for outbound calls
                        "operator": self.phone_session.operator,
                        "greeting": greeting or f"Hello, this is an automated call from {self.phone_session.operator}'s AI assistant."
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        print(f"[PHONE-BRIDGE] Outbound call initiated: {result}")
                        return result
                    else:
                        error = await resp.text()
                        print(f"[PHONE-BRIDGE] Outbound call failed: {resp.status} - {error}")
        except Exception as e:
            print(f"[PHONE-BRIDGE] Outbound call error: {e}")
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
    # Shared Tool Execution
    # =========================================================================

    async def _execute_tool(self, name: str, arguments: Dict) -> str:
        """
        Execute a tool and return the result.

        Uses the unified BlackBoxToolExecutor for common tools (generate_image,
        generate_video, generate_music, web_search, web_fetch, search_memory, etc.)
        and handles phone-specific tools locally.
        """
        from Orchestrator.tools.blackbox_tools import execute_tool as unified_execute_tool

        # Map tool names to unified executor names
        # All tools go through the unified executor for consistency
        unified_tool_map = {
            # Memory/search tools
            "search_snapshots": "search_memory",
            "search_memory": "search_memory",
            "get_snapshot": "get_snapshot",
            "get_recent_snapshots": "list_recent_snapshots",
            "list_recent_snapshots": "list_recent_snapshots",
            # Web tools
            "web_search": "web_search",
            "web_fetch": "web_fetch",
            # Generation tools
            "generate_image": "generate_image",
            "generate_video": "generate_video",
            "generate_music": "generate_music",
            "get_task_status": "get_task_status",
            # Media tools
            "get_media": "get_media",
            "list_media": "list_media",
            "search_media": "search_media",
            # Communication tools
            "send_sms": "send_sms",
            "make_voice_call": "make_voice_call",
            "make_phone_call": "make_phone_call",
            # Utility tools
            "get_current_time": "get_current_time",
            # Contact tools
            "search_contacts": "search_contacts",
            "save_contact": "save_contact",
            # Cron/scheduling tools
            "create_cron_job": "create_cron_job",
            "edit_cron_job": "edit_cron_job",
            "search_cron_jobs": "search_cron_jobs",
            # Computer Use agent
            "use_computer": "use_computer",
            # Device registry
            "list_devices": "list_devices",
            "control_android_device": "control_android_device",
        }

        # Use unified executor for all mapped tools
        if name in unified_tool_map:
            unified_name = unified_tool_map[name]
            result = await unified_execute_tool(unified_name, arguments, self.phone_session.operator)
            return result.rich_result()

        # Unknown tool
        return f"Unknown tool: {name}"

    # =========================================================================
    # Session Saving
    # =========================================================================

    async def _save_session(self):
        """Save the session using existing route save functions.

        Non-blocking: uses timeout to prevent cleanup from hanging if /chat is slow.
        """
        if not self._ai_session:
            return

        backend = self.phone_session.ai_backend

        try:
            save_coro = None
            if backend == AIBackend.OPENAI_REALTIME:
                from Orchestrator.routes.realtime_routes import save_session_to_blackbox
                save_coro = save_session_to_blackbox(self._ai_session)
            elif backend == AIBackend.GEMINI_LIVE:
                from Orchestrator.routes.gemini_live_routes import save_session_to_blackbox
                save_coro = save_session_to_blackbox(self._ai_session)
            elif backend == AIBackend.GROK_LIVE:
                from Orchestrator.routes.grok_live_routes import save_grok_session_to_blackbox
                save_coro = save_grok_session_to_blackbox(self._ai_session)

            if save_coro:
                await asyncio.wait_for(save_coro, timeout=10.0)
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
