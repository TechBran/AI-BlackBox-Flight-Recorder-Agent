#!/usr/bin/env python3
"""
voice_bridge.py — Bridges Asterisk AudioSocket to the Orchestrator's
proven WebSocket streaming pipeline (same one the Android MVP uses).

Architecture:
  AudioSocket (subprocess) ↔ IPC ↔ voice_bridge ↔ WS localhost:9091 → OpenAI 24kHz

Audio flow:
  - Outbound (AI→phone): voice_bridge sends raw 24kHz to subprocess via IPC;
    subprocess handles resampling + pacing + AudioSocket writes.
  - Inbound (phone→AI): subprocess reads AudioSocket, sends 8kHz PCM16 via IPC;
    voice_bridge upsamples to AI rate and forwards to WS.
"""

import asyncio
import base64
import json
import os
import uuid
from typing import Optional

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


# =============================================================================
# Streaming Upsampler (8kHz → 24kHz)
# =============================================================================

_UP_SOS_8_24 = butter(8, 3500, btype='low', fs=24000, output='sos')


class StreamingUpsampler:
    """8kHz→24kHz with anti-imaging filter state preserved across chunks."""

    def __init__(self, source_rate: int = 8000, target_rate: int = 24000):
        self.factor = target_rate // source_rate
        if target_rate == 24000:
            self._sos = _UP_SOS_8_24
        else:
            self._sos = butter(8, source_rate * 0.4375, btype='low', fs=target_rate, output='sos')
        self._zi = sosfilt_zi(self._sos) * 0

    def process(self, pcm16_data: bytes) -> bytes:
        if len(pcm16_data) < 2:
            return b""
        samples = np.frombuffer(pcm16_data, dtype=np.int16).astype(np.float64)
        upsampled = np.zeros(len(samples) * self.factor, dtype=np.float64)
        upsampled[::self.factor] = samples * self.factor
        filtered, self._zi = sosfilt(self._sos, upsampled, zi=self._zi)
        return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()


# =============================================================================
# Voice Bridge
# =============================================================================

class AsteriskVoiceBridge:
    """
    Bridges Asterisk AudioSocket to the Orchestrator's WebSocket streaming
    pipeline — the same pipeline the Android MVP uses.
    """

    ENDPOINTS = {
        "openai_realtime": "/ws/realtime/",
        "gemini_live": "/ws/gemini-live/",
        "grok_live": "/ws/grok-live/",
    }

    INPUT_RATES = {
        "openai_realtime": 24000,
        "gemini_live": 16000,
        "grok_live": 16000,
    }

    OUTPUT_RATE = 24000

    def __init__(
        self,
        ipc_client,
        channel_uuid: str,
        backend: str = "openai_realtime",
        operator: str = "phone-caller",
        voice: str = "",
        asterisk_rate: int = 8000,
        greeting: str = "",
        role: str = "",
    ):
        self._ipc = ipc_client
        self._channel_uuid = channel_uuid
        self._backend = backend
        self._operator = operator
        # Pick sensible default voice per backend if none specified
        if not voice:
            _defaults = {"openai_realtime": "ash", "gemini_live": "Orus", "grok_live": "Rex"}
            voice = _defaults.get(backend, "ash")
        self._voice = voice
        self._asterisk_rate = asterisk_rate
        self._greeting = greeting
        self._role = role

        self._ws = None
        self._running = False
        self._session_id = f"ast-voice-{uuid.uuid4().hex[:12]}"
        self._listener_task = None
        self._http_session = None

        # Streaming upsampler for phone→AI (event loop, lightweight)
        input_rate = self.INPUT_RATES.get(backend, 24000)
        self._upsampler = StreamingUpsampler(asterisk_rate, input_rate)

        # Stats
        self._chunks_sent = 0
        self._chunks_received = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> bool:
        """Connect to the Orchestrator's streaming WebSocket."""
        import aiohttp

        endpoint = self.ENDPOINTS.get(self._backend)
        if not endpoint:
            print(f"[VOICE-BRIDGE] Unknown backend: {self._backend}")
            return False

        ws_url = f"ws://127.0.0.1:9091{endpoint}{self._session_id}"
        print(f"[VOICE-BRIDGE] Connecting to {ws_url}")

        try:
            self._http_session = aiohttp.ClientSession()
            self._ws = await self._http_session.ws_connect(ws_url, timeout=10)

            connect_msg = {
                "type": "connect",
                "operator": self._operator,
                "voice": self._voice,
            }
            if self._greeting:
                connect_msg["greeting"] = self._greeting
            if self._role:
                connect_msg["role"] = self._role
            await self._ws.send_json(connect_msg)

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "connected":
                        print(f"[VOICE-BRIDGE] Connected to {self._backend}")
                        break
                    elif data.get("type") == "error":
                        print(f"[VOICE-BRIDGE] Error: {data.get('data')}")
                        return False
                    elif data.get("type") == "status":
                        continue
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return False

            self._running = True

            self._ipc.register_channel(
                self._channel_uuid,
                on_audio=self._on_phone_audio,
                on_hangup=self._on_phone_hangup,
            )

            self._listener_task = asyncio.create_task(self._ws_listener())

            print(f"[VOICE-BRIDGE] Active: {self._backend} ↔ AudioSocket ({self._asterisk_rate}Hz)")
            return True

        except Exception as e:
            print(f"[VOICE-BRIDGE] Start failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def stop(self):
        """Stop the bridge."""
        self._running = False

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        self._ipc.unregister_channel(self._channel_uuid)

        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_json({"type": "disconnect"})
                await self._ws.close()
            except Exception:
                pass

        if self._http_session:
            await self._http_session.close()

        print(f"[VOICE-BRIDGE] Stopped (sent={self._chunks_sent}, recv={self._chunks_received})")

    # ----- Phone audio → AI -----

    async def _on_phone_audio(self, channel_id: str, pcm16_data: bytes):
        """AudioSocket audio → upsample → base64 → WS."""
        if not self._running or not self._ws or self._ws.closed:
            return

        try:
            pcm16_up = self._upsampler.process(pcm16_data)
            if not pcm16_up:
                return

            audio_b64 = base64.b64encode(pcm16_up).decode('ascii')
            await self._ws.send_json({
                "type": "audio_input",
                "data": audio_b64,
            })

            self._chunks_sent += 1
            if self._chunks_sent == 1:
                input_rate = self.INPUT_RATES.get(self._backend, 24000)
                print(f"[VOICE-BRIDGE] First phone→AI: {len(pcm16_data)}B@{self._asterisk_rate}Hz "
                      f"→ {len(pcm16_up)}B@{input_rate}Hz")

        except Exception as e:
            if "closed" not in str(e).lower():
                print(f"[VOICE-BRIDGE] Send error: {e}")

    async def _on_phone_hangup(self, channel_id: str):
        """Phone hung up."""
        print(f"[VOICE-BRIDGE] Phone hangup")
        self._running = False

    # ----- AI audio → Phone -----

    async def _ws_listener(self):
        """Listen for AI audio, downsample, push to jitter buffer."""
        import aiohttp

        try:
            async for msg in self._ws:
                if not self._running:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_ws_message(data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[VOICE-BRIDGE] Listener error: {e}")
        finally:
            self._running = False

    async def _handle_ws_message(self, data: dict):
        """
        Handle message from Orchestrator WS bridge.

        AI audio is forwarded as raw 24kHz bytes to the audio subprocess via IPC.
        Subprocess handles resampling, pacing, and AudioSocket writes.
        ZERO CPU work on the event loop.
        """
        msg_type = data.get("type", "")

        if msg_type == "audio_delta":
            audio_b64 = data.get("data", "")
            if not audio_b64:
                return

            pcm16_24k = base64.b64decode(audio_b64)

            # Forward raw 24kHz to subprocess — it handles resampling + pacing
            await self._ipc.send_ai_audio(self._channel_uuid, pcm16_24k)

            self._chunks_received += 1
            if self._chunks_received == 1:
                print(f"[VOICE-BRIDGE] First AI audio: {len(pcm16_24k)}B@24kHz → subprocess")

        elif msg_type == "response_complete":
            pass  # Subprocess drains naturally

        elif msg_type == "speech_started":
            await self._ipc.clear_buffer(self._channel_uuid)

        elif msg_type == "user_transcript":
            transcript = data.get("data", "")
            if transcript:
                print(f"[VOICE-BRIDGE] User: {transcript[:100]}")

        elif msg_type in ("transcript_delta", "pong", "speaking", "listening"):
            pass
