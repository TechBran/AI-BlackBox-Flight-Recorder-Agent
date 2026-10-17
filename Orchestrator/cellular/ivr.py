#!/usr/bin/env python3
"""
ivr.py - Local IVR for Cellular Calls

Replaces TwiML <Gather> with direct audio IVR:
- Generates TTS prompts via OpenAI TTS HD
- Plays through audio stream
- Listens for DTMF via AT command URCs (+RXDTMF)
- State machine: PIN → backend selection → operator selection
- Detects call disconnect and bails immediately
"""

import asyncio
import aiohttp
import hashlib
import os
from typing import Optional, Callable, Awaitable

import numpy as np
from scipy.signal import resample

from Orchestrator.phone.audio_converter import AudioConverter

# Cache directory for pre-generated IVR prompts (persistent across restarts)
IVR_CACHE_DIR = os.path.join(os.path.dirname(__file__), "ivr_cache")
os.makedirs(IVR_CACHE_DIR, exist_ok=True)
from Orchestrator.phone.ivr_prompts import (
    IVR_GREETING,
    IVR_WELCOME,
    IVR_RETRY,
    IVR_TIMEOUT,
    IVR_INVALID,
    IVR_PIN_ACCEPTED,
    IVR_PIN_RETRY,
    IVR_PIN_FAILED,
    IVR_PIN_LOCKED,
    IVR_PIN_UNAVAILABLE,
    get_pin_prompt,
    get_confirmation_prompt,
    get_backend_id,
    get_backend_name,
    build_operator_selection_prompt,
    get_operator_confirmation,
    IVR_GOODBYE,
    IVR_OPERATOR_INVALID,
    IVR_TTS_VOICE,
    IVR_TTS_MODEL,
)
from Orchestrator.phone import pin_gate


class CellularIVR:
    """
    Local IVR system for cellular calls.

    Manages the call flow: PIN verification → AI backend selection → operator selection.
    Uses OpenAI TTS for prompts, modem +RXDTMF URCs for digit detection.
    Detects call disconnect via is_call_active callback and exits immediately.
    """

    def __init__(
        self,
        send_audio: Callable[[bytes], Awaitable[None]],
        send_pcm16: Optional[Callable[[bytes], Awaitable[None]]] = None,
        is_call_active: Optional[Callable[[], bool]] = None,
        audio_stream=None,
        timeout_ms: int = 10000,
        max_retries: int = 3,
        caller_id: str = "",
    ):
        """
        Args:
            send_audio: Callback to play ULAW audio to the caller (legacy)
            send_pcm16: Callback to play raw PCM16 8kHz audio directly (preferred)
            is_call_active: Callback to check if call is still connected
            audio_stream: CellularAudioStream for direct barge-in flush control
            timeout_ms: Timeout for first DTMF digit in milliseconds
            max_retries: Max retry attempts per stage
            caller_id: Caller number, keys the cross-call PIN lockout
        """
        self.send_audio = send_audio
        self.send_pcm16 = send_pcm16
        self.is_call_active = is_call_active
        self.audio_stream = audio_stream
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.caller_id = caller_id

        self._dtmf_buffer = ""
        self._dtmf_event = asyncio.Event()

        # Barge-in state
        self._playing = False
        self._barge_in_event = asyncio.Event()

        # DTMF debounce — SIM7600G-H can send duplicate +RXDTMF URCs
        self._last_dtmf_digit = ""
        self._last_dtmf_time = 0.0

    def _check_call(self) -> bool:
        """Check if the call is still active. Returns False if disconnected."""
        if self.is_call_active and not self.is_call_active():
            print("[CELLULAR-IVR] Call disconnected — aborting IVR")
            return False
        return True

    async def on_dtmf_digit(self, digit: str):
        """Called by modem URC handler when a DTMF digit is received."""
        import time
        now = time.monotonic()

        # Debounce: ignore duplicate same-digit within 500ms
        if (digit == self._last_dtmf_digit
                and (now - self._last_dtmf_time) < 0.5):
            print(f"[CELLULAR-IVR] DTMF debounce: ignoring duplicate digit "
                  f"({(now - self._last_dtmf_time)*1000:.0f}ms)")
            return

        self._last_dtmf_digit = digit
        self._last_dtmf_time = now

        # Never log the digit: during the PIN stage it is part of the PIN.
        print(f"[CELLULAR-IVR] DTMF received"
              f"{' (during playback — barge-in)' if self._playing else ''}")
        self._dtmf_buffer += digit
        self._dtmf_event.set()
        # Barge-in: interrupt audio playback if currently playing
        if self._playing:
            self._barge_in_event.set()

    async def _wait_for_dtmf(self, num_digits: int = 1) -> Optional[str]:
        """Wait for DTMF digits with timeout. Checks call state periodically."""
        inter_digit_timeout = 3.0

        if not self._check_call():
            return None

        # Check if digits already arrived during prompt (barge-in)
        if self._dtmf_buffer and len(self._dtmf_buffer) >= num_digits:
            return self._dtmf_buffer[:num_digits]

        if self._dtmf_buffer:
            self._dtmf_event.clear()
        else:
            # Wait for first digit with full timeout, checking call state every 1s
            self._dtmf_event.clear()
            remaining = self.timeout_ms / 1000.0
            while remaining > 0:
                if not self._check_call():
                    return None
                wait_chunk = min(remaining, 1.0)
                try:
                    await asyncio.wait_for(self._dtmf_event.wait(), timeout=wait_chunk)
                    break  # Got a digit
                except asyncio.TimeoutError:
                    remaining -= wait_chunk
                    continue

            if not self._dtmf_buffer:
                return None

        if num_digits == 1:
            return self._dtmf_buffer[:1] if self._dtmf_buffer else None

        # For multi-digit: wait for remaining digits with inter-digit timeout
        while len(self._dtmf_buffer) < num_digits:
            if not self._check_call():
                return None
            self._dtmf_event.clear()
            try:
                await asyncio.wait_for(self._dtmf_event.wait(), timeout=inter_digit_timeout)
            except asyncio.TimeoutError:
                break

        return self._dtmf_buffer[:num_digits] if self._dtmf_buffer else None

    async def _play_prompt(self, text: str, voice: str = IVR_TTS_VOICE,
                           allow_barge_in: bool = True):
        """Generate TTS and play it to the caller with barge-in support.

        When allow_barge_in is True and a DTMF digit arrives during playback,
        audio is immediately flushed and control returns so the digit can be
        processed without waiting for the prompt to finish.
        """
        if not self._check_call():
            return

        # Clear DTMF state BEFORE playback so digits pressed during prompt
        # are captured fresh (barge-in support)
        self._dtmf_buffer = ""
        self._dtmf_event.clear()
        self._barge_in_event.clear()

        try:
            if self.send_pcm16 and self.audio_stream:
                pcm16_8k = await self._generate_tts_pcm16(text, voice)
                if pcm16_8k:
                    print(f"[CELLULAR-IVR] Playing prompt: {len(pcm16_8k)} bytes "
                          f"({len(pcm16_8k)/(8000*2):.1f}s) — \"{text[:50]}...\"")
                    self._playing = True
                    stop = self._barge_in_event if allow_barge_in else None
                    await self.audio_stream.write_pcm16_direct(pcm16_8k, stop_event=stop)
                    interrupted = self._barge_in_event.is_set()
                    self._playing = False
                    if interrupted:
                        print("[CELLULAR-IVR] Barge-in: flushed audio, proceeding to input")
                    else:
                        await asyncio.sleep(0.5)
                else:
                    print(f"[CELLULAR-IVR] TTS returned None for: \"{text[:50]}...\"")
            elif self.send_pcm16:
                # Fallback: no audio_stream reference, play without barge-in
                pcm16_8k = await self._generate_tts_pcm16(text, voice)
                if pcm16_8k:
                    print(f"[CELLULAR-IVR] Playing prompt (no barge-in): {len(pcm16_8k)} bytes")
                    await self.send_pcm16(pcm16_8k)
                    await asyncio.sleep(0.5)
            else:
                ulaw_audio = await self._generate_tts_ulaw(text, voice)
                if ulaw_audio:
                    print(f"[CELLULAR-IVR] Playing ULAW prompt: {len(ulaw_audio)} bytes")
                    await self.send_audio(ulaw_audio)
                    await asyncio.sleep(0.5)
                else:
                    print(f"[CELLULAR-IVR] ULAW TTS returned None for: \"{text[:50]}...\"")
        except Exception as e:
            self._playing = False
            print(f"[CELLULAR-IVR] Prompt error: {e}")
            import traceback
            traceback.print_exc()

    async def _generate_tts_pcm16(self, text: str, voice: str = IVR_TTS_VOICE) -> Optional[bytes]:
        """Generate TTS audio as 8kHz PCM16 with disk caching.
        SIM7600G-H write side works at 8kHz (~14KB/s on ttyUSB4)."""
        cache_key = hashlib.md5(f"{text}:{voice}:{IVR_TTS_MODEL}:8k".encode()).hexdigest()
        cache_path = os.path.join(IVR_CACHE_DIR, f"{cache_key}.pcm16")

        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                pcm_data = f.read()
            if pcm_data:
                print(f"[CELLULAR-IVR] Cache hit: {cache_key[:8]}... "
                      f"({len(pcm_data)} bytes, {len(pcm_data)/(8000*2):.1f}s)")
                return pcm_data

        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

        if not OPENAI_API_KEY:
            print("[CELLULAR-IVR] No OpenAI API key for TTS")
            return None

        try:
            print(f"[CELLULAR-IVR] Generating TTS: \"{text[:60]}...\"")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OPENAI_TTS_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": IVR_TTS_MODEL,
                        "voice": voice,
                        "input": text.strip(),
                        "response_format": "pcm"
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        error_body = await resp.text()
                        print(f"[CELLULAR-IVR] TTS error {resp.status}: {error_body[:200]}")
                        return None

                    pcm_24k = await resp.read()

            # Resample: 24kHz → 8kHz (modem write side works at 8kHz)
            samples_24k = np.frombuffer(pcm_24k, dtype=np.int16)
            num_8k = int(len(samples_24k) * 8000 / 24000)
            samples_8k = resample(samples_24k.astype(np.float64), num_8k)
            pcm_8k = np.clip(samples_8k, -32768, 32767).astype(np.int16).tobytes()

            with open(cache_path, "wb") as f:
                f.write(pcm_8k)
            duration = len(pcm_8k) / (8000 * 2)
            print(f"[CELLULAR-IVR] TTS cached: {len(pcm_8k)} bytes ({duration:.1f}s @ 8kHz)")

            return pcm_8k

        except Exception as e:
            print(f"[CELLULAR-IVR] TTS PCM16 generation error: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _generate_tts_ulaw(self, text: str, voice: str = IVR_TTS_VOICE) -> Optional[bytes]:
        """Generate TTS audio and convert to ULAW for phone playback."""
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

        if not OPENAI_API_KEY:
            print("[CELLULAR-IVR] No OpenAI API key for TTS")
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
                        "model": IVR_TTS_MODEL,
                        "voice": voice,
                        "input": text.strip(),
                        "response_format": "pcm"
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        print(f"[CELLULAR-IVR] TTS error: {resp.status}")
                        return None

                    pcm_24k = await resp.read()
                    return AudioConverter.ai_to_phone(pcm_24k, source_rate=24000)

        except Exception as e:
            print(f"[CELLULAR-IVR] TTS generation error: {e}")
            return None

    async def _refuse_if_locked(self) -> bool:
        """Refuse when this caller (or every caller) is locked out right now."""
        locked_for = pin_gate.lockout_remaining(self.caller_id)
        if not locked_for:
            return False
        print(f"[CELLULAR-IVR] Caller {pin_gate.mask_caller(self.caller_id)} "
              f"is locked out ({locked_for}s left), refusing")
        await self._play_prompt(IVR_PIN_LOCKED, allow_barge_in=False)
        return True

    async def run_pin_verification(self) -> bool:
        """Run PIN verification stage. Fails closed; never logs the PIN or digits."""
        status = pin_gate.gate_status()
        if status == "disabled":
            print("[CELLULAR-IVR] PIN disabled (PHONE_PIN_ENABLED=false), skipping")
            return True
        if status == "unconfigured":
            pin_gate.log_unconfigured("CELLULAR-IVR")
            await self._play_prompt(IVR_PIN_UNAVAILABLE, allow_barge_in=False)
            return False

        if await self._refuse_if_locked():
            return False

        pin_len = pin_gate.pin_length()
        attempts = pin_gate.max_attempts()
        print(f"[CELLULAR-IVR] Starting PIN verification ({pin_len} digits, {attempts} attempts)")

        # Play greeting on first call
        await self._play_prompt(IVR_GREETING, allow_barge_in=False)

        for attempt in range(attempts):
            if not self._check_call():
                return False
            # Re-check before each retry and before judging each entry: a
            # parallel call from the same number (or the global backstop) may
            # have locked it while this call was waiting.
            if attempt and await self._refuse_if_locked():
                return False

            if attempt == 0:
                await self._play_prompt(get_pin_prompt(pin_len))
            else:
                await self._play_prompt(IVR_PIN_RETRY)

            digits = await self._wait_for_dtmf(num_digits=pin_len)

            if await self._refuse_if_locked():
                return False

            if pin_gate.check_pin(digits):
                pin_gate.record_success(self.caller_id)
                await self._play_prompt(IVR_PIN_ACCEPTED, allow_barge_in=False)
                return True

            if not digits:
                print(f"[CELLULAR-IVR] PIN attempt {attempt+1}/{attempts}: no input")
                continue
            print(f"[CELLULAR-IVR] PIN attempt {attempt+1}/{attempts} rejected")
            if pin_gate.record_failure(self.caller_id):
                await self._play_prompt(IVR_PIN_LOCKED, allow_barge_in=False)
                return False

        await self._play_prompt(IVR_PIN_FAILED, allow_barge_in=False)
        return False

    async def run_backend_selection(self) -> Optional[str]:
        """Run AI backend selection stage."""
        from Orchestrator.config import IVR_DEFAULT_BACKEND

        print(f"[CELLULAR-IVR] Starting backend selection (default: {IVR_DEFAULT_BACKEND})")

        for attempt in range(self.max_retries):
            if not self._check_call():
                return IVR_DEFAULT_BACKEND

            if attempt == 0:
                await self._play_prompt(IVR_WELCOME)
            else:
                await self._play_prompt(IVR_RETRY)

            digit = await self._wait_for_dtmf(num_digits=1)

            if digit and digit.isdigit():
                selection = int(digit)
                if 1 <= selection <= 4:
                    backend_id = get_backend_id(selection)
                    backend_name = get_backend_name(selection)
                    await self._play_prompt(get_confirmation_prompt(selection),
                                            allow_barge_in=False)
                    print(f"[CELLULAR-IVR] Backend selected: {backend_name} ({backend_id})")
                    return backend_id
                else:
                    # Invalid digit (0, 5-9) — give feedback
                    print("[CELLULAR-IVR] Invalid backend selection")
                    await self._play_prompt(IVR_INVALID)
                    continue
            elif digit:
                # Non-digit character (*, #)
                print("[CELLULAR-IVR] Non-digit backend input")
                await self._play_prompt(IVR_INVALID)
                continue

        await self._play_prompt(IVR_TIMEOUT, allow_barge_in=False)
        print(f"[CELLULAR-IVR] Defaulting to: {IVR_DEFAULT_BACKEND}")
        return IVR_DEFAULT_BACKEND

    async def run_operator_selection(self) -> Optional[str]:
        """Run operator selection stage."""
        from Orchestrator.config import USERS_LIST

        if not USERS_LIST or len(USERS_LIST) <= 1:
            operator = USERS_LIST[0] if USERS_LIST else "Brandon"
            print(f"[CELLULAR-IVR] Single operator, skipping selection: {operator}")
            return operator

        prompt_text = build_operator_selection_prompt(USERS_LIST)

        for attempt in range(self.max_retries):
            if not self._check_call():
                return USERS_LIST[0]

            if attempt == 0:
                await self._play_prompt(prompt_text)
            else:
                await self._play_prompt("I didn't catch that. " + prompt_text)

            digit = await self._wait_for_dtmf(num_digits=1)

            if digit and digit.isdigit():
                selection = int(digit)
                if 1 <= selection <= len(USERS_LIST):
                    operator = USERS_LIST[selection - 1]
                    print(f"[CELLULAR-IVR] Operator selected: {operator}")
                    return operator
                else:
                    print("[CELLULAR-IVR] Invalid operator selection")
                    await self._play_prompt(IVR_OPERATOR_INVALID)
                    continue
            elif digit:
                print("[CELLULAR-IVR] Non-digit operator input")
                await self._play_prompt(IVR_OPERATOR_INVALID)
                continue

        return USERS_LIST[0]

    async def run_full_ivr(self) -> Optional[dict]:
        """Run the complete IVR flow: PIN → operator → backend."""
        print("[CELLULAR-IVR] === Starting full IVR flow ===")

        # Stage 1: PIN verification
        if not await self.run_pin_verification():
            print("[CELLULAR-IVR] PIN verification failed")
            return None

        if not self._check_call():
            return None

        # Stage 2: Operator selection
        operator = await self.run_operator_selection()
        if not operator:
            return None

        if not self._check_call():
            return None

        # Brief pause to drain any pending modem URCs (prevents DTMF bleed)
        await asyncio.sleep(0.3)
        self._dtmf_buffer = ""
        self._dtmf_event.clear()
        self._barge_in_event.clear()

        # Stage 3: Backend selection
        backend = await self.run_backend_selection()
        if not backend:
            return None

        # Confirm and return
        backend_name = {
            "claude_code": "Claude Code",
            "gemini_live": "Gemini",
            "openai_realtime": "GPT",
            "grok_live": "Grok"
        }.get(backend, backend)

        if self._check_call():
            await self._play_prompt(get_operator_confirmation(operator, backend_name),
                                    allow_barge_in=False)

        print(f"[CELLULAR-IVR] === IVR complete: operator={operator}, backend={backend} ===")

        return {
            "operator": operator,
            "backend": backend,
        }
