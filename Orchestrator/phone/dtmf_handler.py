#!/usr/bin/env python3
"""
dtmf_handler.py - IVR Menu and DTMF Detection

Handles DTMF (Dual-Tone Multi-Frequency) digit detection for IVR navigation.
Works with FreeSwitch ESL events to detect user keypad input.

IVR Flow:
1. Play welcome prompt
2. Wait for DTMF input (1-4)
3. On valid input: confirm and bridge to AI backend
4. On invalid/timeout: retry up to MAX_RETRIES times
5. On max retries: default to configured backend
"""

import asyncio
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass

from Orchestrator.phone.session import PhoneSession, PhoneStatus, AIBackend
from Orchestrator.phone.ivr_prompts import (
    IVR_WELCOME,
    IVR_RETRY,
    IVR_INVALID,
    IVR_TIMEOUT,
    get_confirmation_prompt,
    get_backend_id,
    get_backend_name,
)


# DTMF digit to integer mapping
DTMF_DIGITS = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "*": 10, "#": 11,
}

# Valid IVR selections
VALID_SELECTIONS = {1, 2, 3, 4}


@dataclass
class DTMFEvent:
    """Represents a DTMF digit event from FreeSwitch."""
    digit: str
    duration_ms: int = 0
    timestamp: str = ""


class IVRState:
    """
    Manages IVR state machine for a phone session.

    States:
    - WAITING_INPUT: Playing prompt, waiting for DTMF
    - PROCESSING: Validating input
    - CONFIRMING: Playing confirmation
    - BRIDGING: Connecting to AI backend
    - COMPLETED: IVR complete, call bridged
    - FAILED: Max retries exceeded or error
    """

    def __init__(
        self,
        session: PhoneSession,
        timeout_ms: int = 5000,
        max_retries: int = 3,
        default_backend: str = "openai_realtime",
    ):
        self.session = session
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.default_backend = default_backend

        self.current_retry = 0
        self.input_buffer = ""
        self._timeout_task: Optional[asyncio.Task] = None

        # Callbacks
        self.on_play_prompt: Optional[Callable[[str], Awaitable[None]]] = None
        self.on_selection_complete: Optional[Callable[[int, str], Awaitable[None]]] = None

    async def start(self):
        """Start the IVR flow by playing the welcome prompt."""
        self.session.status = PhoneStatus.IVR
        await self._play_prompt(IVR_WELCOME)
        self._start_timeout()

    async def handle_dtmf(self, event: DTMFEvent):
        """
        Handle a DTMF digit event.

        Args:
            event: DTMFEvent with the pressed digit
        """
        # Cancel any pending timeout
        self._cancel_timeout()

        digit = event.digit
        print(f"[IVR] Session {self.session.session_id}: DTMF digit received")

        # Check if valid digit
        if digit not in DTMF_DIGITS:
            print("[IVR] Invalid DTMF character")
            await self._handle_invalid()
            return

        digit_value = DTMF_DIGITS[digit]

        # Check if valid selection (1-4)
        if digit_value in VALID_SELECTIONS:
            await self._handle_valid_selection(digit_value)
        else:
            await self._handle_invalid()

    async def handle_timeout(self):
        """Handle IVR timeout (no input received)."""
        print(f"[IVR] Session {self.session.session_id}: Timeout (retry {self.current_retry + 1}/{self.max_retries})")

        self.current_retry += 1

        if self.current_retry >= self.max_retries:
            # Max retries - use default backend
            await self._use_default_backend()
        else:
            # Retry with prompt
            await self._play_prompt(IVR_RETRY)
            self._start_timeout()

    async def _handle_valid_selection(self, selection: int):
        """Handle a valid menu selection."""
        print(f"[IVR] Session {self.session.session_id}: Valid selection {selection}")

        self.session.ivr_selection = selection
        backend_id = get_backend_id(selection)
        backend_name = get_backend_name(selection)

        # Map to AIBackend enum
        backend_map = {
            "claude_code": AIBackend.CLAUDE_CODE,
            "gemini_live": AIBackend.GEMINI_LIVE,
            "openai_realtime": AIBackend.OPENAI_REALTIME,
            "grok_live": AIBackend.GROK_LIVE,
        }
        self.session.ai_backend = backend_map.get(backend_id, AIBackend.OPENAI_REALTIME)

        # Play confirmation
        confirmation = get_confirmation_prompt(selection)
        await self._play_prompt(confirmation)

        # Signal selection complete
        if self.on_selection_complete:
            await self.on_selection_complete(selection, backend_id)

    async def _handle_invalid(self):
        """Handle invalid input."""
        print(f"[IVR] Session {self.session.session_id}: Invalid selection (retry {self.current_retry + 1}/{self.max_retries})")

        self.current_retry += 1

        if self.current_retry >= self.max_retries:
            await self._use_default_backend()
        else:
            await self._play_prompt(IVR_INVALID)
            self._start_timeout()

    async def _use_default_backend(self):
        """Use the default backend after max retries."""
        print(f"[IVR] Session {self.session.session_id}: Using default backend: {self.default_backend}")

        # Map default backend string to selection
        backend_to_selection = {
            "claude_code": 1,
            "gemini_live": 2,
            "openai_realtime": 3,
            "grok_live": 4,
        }
        selection = backend_to_selection.get(self.default_backend, 3)
        self.session.ivr_selection = selection

        # Map to AIBackend enum
        backend_map = {
            "claude_code": AIBackend.CLAUDE_CODE,
            "gemini_live": AIBackend.GEMINI_LIVE,
            "openai_realtime": AIBackend.OPENAI_REALTIME,
            "grok_live": AIBackend.GROK_LIVE,
        }
        self.session.ai_backend = backend_map.get(self.default_backend, AIBackend.OPENAI_REALTIME)

        # Play timeout message
        await self._play_prompt(IVR_TIMEOUT)

        # Signal selection complete
        if self.on_selection_complete:
            await self.on_selection_complete(selection, self.default_backend)

    async def _play_prompt(self, text: str):
        """Play a TTS prompt to the caller."""
        if self.on_play_prompt:
            await self.on_play_prompt(text)
        else:
            print(f"[IVR] Would play prompt: {text[:50]}...")

    def _start_timeout(self):
        """Start the input timeout timer."""
        self._cancel_timeout()

        async def timeout_handler():
            await asyncio.sleep(self.timeout_ms / 1000.0)
            await self.handle_timeout()

        self._timeout_task = asyncio.create_task(timeout_handler())

    def _cancel_timeout(self):
        """Cancel the pending timeout timer."""
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None


def parse_freeswitch_dtmf(event_data: dict) -> Optional[DTMFEvent]:
    """
    Parse a FreeSwitch ESL DTMF event.

    Args:
        event_data: FreeSwitch event dictionary

    Returns:
        DTMFEvent if valid, None otherwise
    """
    event_name = event_data.get("Event-Name", "")
    if event_name != "DTMF":
        return None

    digit = event_data.get("DTMF-Digit", "")
    if not digit:
        return None

    duration = int(event_data.get("DTMF-Duration", 0))

    return DTMFEvent(
        digit=digit,
        duration_ms=duration,
    )


def is_valid_ivr_selection(digit: str) -> bool:
    """Check if a DTMF digit is a valid IVR selection (1-4)."""
    return digit in {"1", "2", "3", "4"}
