#!/usr/bin/env python3
"""
ivr.py — Asterisk IVR Engine

Runs PIN verification → Operator selection → Backend selection
while the channel is in ARI Stasis. Uses ARI DTMF events and
pre-generated WAV file playback.

After IVR completes, the caller returns a dict with operator/backend/voice
and the handler continues the channel to AudioSocket for AI conversation.
"""

import asyncio
import traceback
from typing import Optional, Dict

from Orchestrator.asterisk.ivr_audio import (
    get_prompt_uri, generate_operator_menu, filter_ivr_operators,
    pin_prompt_name, resolve_prompt,
)
from Orchestrator.phone import pin_gate


class AsteriskIVR:
    """
    IVR system for Asterisk inbound calls.
    Runs while channel is in ARI Stasis.
    """

    # Backend digit mapping (same as ivr_prompts.py)
    BACKEND_MAP = {
        "1": ("claude_code", "Claude"),
        "2": ("gemini_live", "Gemini"),
        "3": ("openai_realtime", "GPT"),
        "4": ("grok_live", "Grok"),
    }

    # Default voices per backend
    BACKEND_VOICES = {
        "claude_code": "onyx",
        "gemini_live": "Charon",
        "openai_realtime": "ash",
        "grok_live": "Ara",
    }

    def __init__(self, ari_client, channel_id: str, caller_id: str = ""):
        self._client = ari_client
        self._channel_id = channel_id
        self._caller_id = caller_id

        # DTMF state
        self._dtmf_buffer = ""
        self._dtmf_event = asyncio.Event()

        # Playback state (for barge-in)
        self._current_playback_id: Optional[str] = None
        self._barge_in = False

        # Save/restore original DTMF handler
        self._original_on_dtmf = None

    async def run(self) -> Optional[Dict[str, str]]:
        """
        Run the full IVR flow.
        Returns {"operator": str, "backend": str, "voice": str} or None on failure.
        """
        print(f"[ASTERISK-IVR] Starting IVR for {self._caller_id} on channel {self._channel_id}")

        # Register our DTMF handler (save original to restore later)
        self._original_on_dtmf = self._client.on_dtmf
        self._client.on_dtmf = self._on_dtmf

        try:
            # Stage 1: PIN verification (plays its own refusal on failure)
            pin_ok = await self._run_pin()
            if not pin_ok:
                return None

            # Stage 2: Operator selection
            operator = await self._run_operator_select()
            if not operator:
                return None

            # Clear DTMF buffer between stages
            self._dtmf_buffer = ""
            self._dtmf_event.clear()
            await asyncio.sleep(0.3)
            self._dtmf_buffer = ""

            # Stage 3: Backend selection
            backend = await self._run_backend_select()
            if not backend:
                return None

            # Get voice for selected backend (check operator preference first)
            try:
                from Orchestrator.state import get_operator_preference
                voice = get_operator_preference(operator, "voice", self.BACKEND_VOICES.get(backend, "ash"))
            except Exception:
                voice = self.BACKEND_VOICES.get(backend, "ash")

            # Play confirmation
            confirm_name = {
                "claude_code": "confirm_claude",
                "gemini_live": "confirm_gemini",
                "openai_realtime": "confirm_gpt",
                "grok_live": "confirm_grok",
            }.get(backend, "connecting")
            await self._play(get_prompt_uri(confirm_name))
            await asyncio.sleep(1.5)  # Let confirmation play

            print(f"[ASTERISK-IVR] IVR complete: operator={operator}, backend={backend}, voice={voice}")
            return {"operator": operator, "backend": backend, "voice": voice}

        except Exception as e:
            print(f"[ASTERISK-IVR] Error: {e}")
            traceback.print_exc()
            return None
        finally:
            # Restore original DTMF handler
            self._client.on_dtmf = self._original_on_dtmf

    # ----- DTMF handling -----

    async def _on_dtmf(self, channel_id: str, digit: str):
        """Handle DTMF from ARI — only for our channel."""
        if channel_id != self._channel_id:
            # Not our channel — forward to original handler
            if self._original_on_dtmf:
                await self._original_on_dtmf(channel_id, digit)
            return

        # Never log the digit: during the PIN stage it is part of the PIN.
        print(f"[ASTERISK-IVR] DTMF received{' (barge-in)' if self._current_playback_id else ''}")
        self._dtmf_buffer += digit
        self._dtmf_event.set()

        # Barge-in: stop current playback
        if self._current_playback_id:
            try:
                await self._client.stop_playback(self._current_playback_id)
            except Exception:
                pass
            self._current_playback_id = None
            self._barge_in = True

    async def _wait_dtmf(self, num_digits: int = 1, timeout_s: float = 10.0) -> Optional[str]:
        """Wait for DTMF digits with timeout."""
        # Check if digits already buffered (from barge-in during prompt)
        if len(self._dtmf_buffer) >= num_digits:
            result = self._dtmf_buffer[:num_digits]
            self._dtmf_buffer = self._dtmf_buffer[num_digits:]
            return result

        # Wait for first digit with full timeout
        self._dtmf_event.clear()
        remaining = timeout_s
        while remaining > 0:
            try:
                await asyncio.wait_for(self._dtmf_event.wait(), timeout=min(remaining, 1.0))
                break  # Got a digit
            except asyncio.TimeoutError:
                remaining -= 1.0

        if not self._dtmf_buffer:
            return None

        # For multi-digit: wait for remaining with inter-digit timeout
        inter_digit_timeout = 3.0
        while len(self._dtmf_buffer) < num_digits:
            self._dtmf_event.clear()
            try:
                await asyncio.wait_for(self._dtmf_event.wait(), timeout=inter_digit_timeout)
            except asyncio.TimeoutError:
                break

        if len(self._dtmf_buffer) >= num_digits:
            result = self._dtmf_buffer[:num_digits]
            self._dtmf_buffer = self._dtmf_buffer[num_digits:]
            return result

        return self._dtmf_buffer if self._dtmf_buffer else None

    # ----- Playback -----

    async def _play(self, media_uri: str):
        """Play a sound via ARI. Supports barge-in (DTMF stops playback).

        Uses ARI PlaybackFinished event for precise completion detection
        instead of polling with fixed sleep.
        """
        self._barge_in = False
        playback_id = await self._client.play_sound(self._channel_id, media_uri)
        if playback_id:
            self._current_playback_id = playback_id
            # Wait for PlaybackFinished event or barge-in, whichever comes first
            # Use a background task so barge-in DTMF can interrupt
            wait_task = asyncio.create_task(
                self._client.wait_for_playback(playback_id, timeout=15.0)
            )
            try:
                while not wait_task.done():
                    if self._barge_in:
                        wait_task.cancel()
                        break
                    await asyncio.sleep(0.05)  # 50ms check interval for barge-in
            except asyncio.CancelledError:
                wait_task.cancel()
            self._current_playback_id = None

    async def _play_and_wait(self, media_uri: str, num_digits: int = 1,
                              timeout_s: float = 10.0) -> Optional[str]:
        """Play prompt then wait for DTMF. Barge-in supported."""
        self._dtmf_buffer = ""
        self._dtmf_event.clear()

        await self._play(media_uri)

        # If barge-in happened, digits are already in buffer
        if self._dtmf_buffer and len(self._dtmf_buffer) >= num_digits:
            result = self._dtmf_buffer[:num_digits]
            self._dtmf_buffer = self._dtmf_buffer[num_digits:]
            return result

        # Otherwise wait for more digits
        return await self._wait_dtmf(num_digits, timeout_s)

    # ----- IVR Stages -----

    async def _refuse(self, prompt: str):
        """Play a refusal (or its installed fallback) and let it finish; the
        caller hangs up on None."""
        await self._play(get_prompt_uri(resolve_prompt(prompt)))
        await asyncio.sleep(1.5)  # Let prompt finish

    async def _refuse_if_locked(self) -> bool:
        """Refuse when this caller (or every caller) is locked out right now."""
        locked_for = pin_gate.lockout_remaining(self._caller_id)
        if not locked_for:
            return False
        print(f"[ASTERISK-IVR] Caller {pin_gate.mask_caller(self._caller_id)} "
              f"is locked out ({locked_for}s left), refusing")
        await self._refuse("pin_locked")
        return True

    async def _run_pin(self) -> bool:
        """PIN verification stage. Fails closed; never logs the PIN or digits."""
        status = pin_gate.gate_status()
        if status == "disabled":
            print("[ASTERISK-IVR] PIN disabled (PHONE_PIN_ENABLED=false), skipping")
            return True
        if status == "unconfigured":
            pin_gate.log_unconfigured("ASTERISK-IVR")
            await self._refuse("pin_unavailable")
            return False

        if await self._refuse_if_locked():
            return False

        pin_len = pin_gate.pin_length()
        attempts = pin_gate.max_attempts()
        print(f"[ASTERISK-IVR] PIN verification ({pin_len} digits, {attempts} attempts)")
        # Existence check only: no audio is made or copied while a caller waits.
        prompt = pin_prompt_name(pin_len)

        # Play greeting first
        await self._play(get_prompt_uri("greeting"))
        await asyncio.sleep(0.5)

        for attempt in range(attempts):
            # Re-check before each retry and before judging each entry: a
            # parallel call from the same number (or the global backstop) may
            # have locked it while this call was waiting.
            if attempt and await self._refuse_if_locked():
                return False
            self._dtmf_buffer = ""
            self._dtmf_event.clear()

            digits = await self._play_and_wait(
                get_prompt_uri(prompt if attempt == 0 else "pin_retry"),
                num_digits=pin_len, timeout_s=15.0)

            if await self._refuse_if_locked():
                return False

            if pin_gate.check_pin(digits):
                pin_gate.record_success(self._caller_id)
                await self._play(get_prompt_uri("pin_accepted"))
                await asyncio.sleep(1.0)
                print(f"[ASTERISK-IVR] PIN accepted on attempt {attempt + 1}")
                return True

            if not digits:
                print(f"[ASTERISK-IVR] PIN attempt {attempt + 1}/{attempts}: no input")
                continue
            print(f"[ASTERISK-IVR] PIN attempt {attempt + 1}/{attempts} rejected")
            if pin_gate.record_failure(self._caller_id):
                await self._refuse("pin_locked")
                return False

        print("[ASTERISK-IVR] PIN failed after max attempts")
        await self._refuse("pin_failed")
        return False

    async def _run_operator_select(self) -> Optional[str]:
        """Operator selection stage."""
        from Orchestrator.config import USERS_LIST, current_default

        # Filter to real human operators for IVR
        ivr_operators = filter_ivr_operators(USERS_LIST) if USERS_LIST else []

        if not ivr_operators or len(ivr_operators) <= 1:
            operator = ivr_operators[0] if ivr_operators else (USERS_LIST[0] if USERS_LIST else current_default())
            print(f"[ASTERISK-IVR] Single operator, auto-selecting: {operator}")
            return operator

        # Generate dynamic operator menu prompt
        menu_name = await generate_operator_menu(USERS_LIST)

        for attempt in range(3):
            self._dtmf_buffer = ""
            self._dtmf_event.clear()

            digit = await self._play_and_wait(get_prompt_uri(menu_name),
                                               num_digits=1, timeout_s=10.0)

            if digit and digit.isdigit():
                idx = int(digit)
                if 1 <= idx <= len(ivr_operators):
                    operator = ivr_operators[idx - 1]
                    print(f"[ASTERISK-IVR] Operator selected: {operator}")
                    return operator
                else:
                    await self._play(get_prompt_uri("operator_invalid"))
                    await asyncio.sleep(0.5)
            elif digit:
                await self._play(get_prompt_uri("operator_invalid"))
                await asyncio.sleep(0.5)

        # Timeout — default to first operator
        operator = ivr_operators[0]
        await self._play(get_prompt_uri("operator_timeout"))
        await asyncio.sleep(1.0)
        print(f"[ASTERISK-IVR] Operator timeout, defaulting to: {operator}")
        return operator

    async def _run_backend_select(self) -> Optional[str]:
        """AI backend selection stage."""
        from Orchestrator.config import IVR_DEFAULT_BACKEND

        for attempt in range(3):
            self._dtmf_buffer = ""
            self._dtmf_event.clear()

            if attempt == 0:
                digit = await self._play_and_wait(get_prompt_uri("backend_menu"),
                                                   num_digits=1, timeout_s=10.0)
            else:
                digit = await self._play_and_wait(get_prompt_uri("backend_retry"),
                                                   num_digits=1, timeout_s=10.0)

            if digit and digit in self.BACKEND_MAP:
                backend_id, backend_name = self.BACKEND_MAP[digit]
                print(f"[ASTERISK-IVR] Backend selected: {backend_name} ({backend_id})")
                return backend_id
            elif digit:
                print("[ASTERISK-IVR] Invalid backend selection")
                await self._play(get_prompt_uri("backend_invalid"))
                await asyncio.sleep(0.5)

        # Timeout — use default
        await self._play(get_prompt_uri("backend_timeout"))
        await asyncio.sleep(1.0)
        print(f"[ASTERISK-IVR] Backend timeout, defaulting to: {IVR_DEFAULT_BACKEND}")
        return IVR_DEFAULT_BACKEND
