"""
IVR Audio Generator — TTS prompt generation for Asterisk IVR menus.

Generates 8kHz 16-bit mono WAV files from OpenAI TTS-1-HD (onyx voice).
Audio is generated at 24kHz PCM16, then downsampled to 8kHz using a
streaming Butterworth anti-alias filter (same as voice_bridge.py).

Files are saved to:
  - /usr/share/asterisk/sounds/custom/ivr/  (Asterisk plays via ARI)
  - Orchestrator/asterisk/ivr_cache/        (local backup)

Asterisk ARI plays prompts as: sound:custom/ivr/{name}  (no .wav extension)
"""

import asyncio
import hashlib
import os
import shutil
import subprocess
import traceback
import wave
from typing import List, Optional

import aiohttp
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
ASTERISK_SOUND_DIR = "/usr/share/asterisk/sounds/en/custom/ivr"
LOCAL_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ivr_cache")

# ---------------------------------------------------------------------------
# TTS config
# ---------------------------------------------------------------------------
TTS_VOICE = "onyx"
TTS_MODEL = "tts-1-hd"

# ---------------------------------------------------------------------------
# Anti-alias filter for 24kHz -> 8kHz  (same as voice_bridge.py)
# 8th-order Butterworth, cutoff 3500 Hz at 24 kHz sample rate
# ---------------------------------------------------------------------------
_DOWN_SOS = butter(8, 3500, btype='low', fs=24000, output='sos')


def _resample_24k_to_8k(pcm16_24k: bytes) -> bytes:
    """Downsample 24kHz PCM16 to 8kHz using Butterworth low-pass filter."""
    samples = np.frombuffer(pcm16_24k, dtype=np.int16).astype(np.float64)
    zi = sosfilt_zi(_DOWN_SOS) * 0
    filtered, _ = sosfilt(_DOWN_SOS, samples, zi=zi)
    # Decimate by 3 (24000 / 3 = 8000)
    decimated = filtered[::3]
    return np.clip(decimated, -32768, 32767).astype(np.int16).tobytes()


def _save_wav(filepath: str, pcm16_8k: bytes):
    """Save PCM16 8kHz mono data as a WAV file."""
    with wave.open(filepath, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(pcm16_8k)


def _get_openai_config():
    """Import OpenAI config, handling both package and direct import paths."""
    try:
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL
    except ImportError:
        try:
            from config import OPENAI_API_KEY, OPENAI_TTS_URL
        except ImportError:
            # Last-resort fallback if neither import path works (should never happen
            # in production; central config import is the authoritative source).
            OPENAI_API_KEY = ""
            OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
    return OPENAI_API_KEY, OPENAI_TTS_URL


async def generate_prompt(name: str, text: str, voice: str = TTS_VOICE) -> bool:
    """Generate a single TTS prompt and save as WAV.

    Returns True if successful (or already cached in Asterisk sounds).
    """
    asterisk_path = os.path.join(ASTERISK_SOUND_DIR, f"{name}.wav")
    local_path = os.path.join(LOCAL_CACHE_DIR, f"{name}.wav")

    # --- Already in Asterisk sounds? ---
    if os.path.exists(asterisk_path) and os.path.getsize(asterisk_path) > 100:
        return True

    # --- In local cache? Copy to Asterisk sounds ---
    if os.path.exists(local_path) and os.path.getsize(local_path) > 100:
        try:
            os.makedirs(ASTERISK_SOUND_DIR, exist_ok=True)
            shutil.copy2(local_path, asterisk_path)
            return True
        except PermissionError:
            os.system(f'sudo mkdir -p "{ASTERISK_SOUND_DIR}"')
            os.system(f'sudo cp "{local_path}" "{asterisk_path}"')
            return os.path.exists(asterisk_path)

    # --- Generate via OpenAI TTS ---
    api_key, tts_url = _get_openai_config()
    if not api_key:
        print("[IVR-AUDIO] No OpenAI API key — cannot generate TTS")
        return False

    try:
        display_text = text[:60] + ("..." if len(text) > 60 else "")
        print(f'[IVR-AUDIO] Generating: {name} — "{display_text}"')

        async with aiohttp.ClientSession() as session:
            async with session.post(
                tts_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": TTS_MODEL,
                    "voice": voice,
                    "input": text.strip(),
                    "response_format": "pcm",  # raw 24kHz PCM16
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    print(f"[IVR-AUDIO] TTS error {resp.status}: {error[:200]}")
                    return False
                pcm_24k = await resp.read()

        if len(pcm_24k) < 100:
            print(f"[IVR-AUDIO] TTS returned too little data ({len(pcm_24k)} bytes)")
            return False

        # Downsample 24kHz -> 8kHz
        pcm_8k = _resample_24k_to_8k(pcm_24k)
        duration = len(pcm_8k) / (8000 * 2)
        print(f"[IVR-AUDIO] Generated: {name} ({duration:.1f}s, {len(pcm_8k)} bytes)")

        # Save to local cache
        os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
        _save_wav(local_path, pcm_8k)

        # Copy to Asterisk sounds directory (always use sudo — dir is root-owned)
        subprocess.run(['sudo', 'mkdir', '-p', ASTERISK_SOUND_DIR], capture_output=True)
        result = subprocess.run(['sudo', 'cp', local_path, asterisk_path], capture_output=True)
        if result.returncode != 0:
            print(f"[IVR-AUDIO] Failed to copy to Asterisk dir: {result.stderr.decode()}")
        elif not os.path.exists(asterisk_path):
            print(f"[IVR-AUDIO] WARNING: cp succeeded but file not found at {asterisk_path}")

        return True

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[IVR-AUDIO] Error generating {name}: {e}")
        traceback.print_exc()
        return False


def get_prompt_uri(name: str) -> str:
    """Get the Asterisk sound URI for a prompt (used with ARI playMedia)."""
    return f"sound:ivr/{name}"


def pin_prompt_text(length: int) -> str:
    """PIN prompt wording for a ``length``-digit PIN."""
    return f"Please enter your {length}-digit access code."


async def ensure_pin_prompt(length: int) -> str:
    """Return the prompt name for a ``length``-digit PIN, generating it once.

    The name carries the length because the cache is keyed by name, not text
    (a changed PIN length must never replay a stale "N-digit" recording).
    Falls back to the length-free ``pin_entry`` prompt if TTS is unavailable.
    """
    name = f"pin_prompt_{length}digit"
    if await generate_prompt(name, pin_prompt_text(length)):
        return name
    return "pin_entry"


def filter_ivr_operators(operators: List[str], max_operators: int = 4) -> List[str]:
    """Filter operator list to human operators suitable for IVR menu.

    Excludes test accounts, dev accounts, and system operators.
    Limits to max_operators to keep the phone menu short.
    """
    exclude_substrings = ["test", "dev", "research", "fa tech", "system", "bot", "agent"]
    filtered = []
    for op in operators:
        op_lower = op.lower()
        if any(ex in op_lower for ex in exclude_substrings):
            continue
        filtered.append(op)
    return filtered[:max_operators]


async def generate_operator_menu(operators: List[str]) -> str:
    """Generate dynamic operator selection prompt. Returns the prompt name.

    The prompt is cached based on an MD5 hash of the text, so it only
    regenerates when the operator list changes.
    """
    if not operators:
        return "operator_timeout"

    # Filter to real human operators for IVR
    ivr_operators = filter_ivr_operators(operators)
    if not ivr_operators:
        return "operator_timeout"

    # Build prompt text
    lines = ["Please identify yourself."]
    for i, op in enumerate(ivr_operators[:9], 1):
        lines.append(f"Press {i} for {op}.")
    text = " ".join(lines)

    # Hash for caching (changes when operator list changes)
    text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
    name = f"operator_menu_{text_hash}"

    await generate_prompt(name, text)
    return name


# ---------------------------------------------------------------------------
# Static prompts — pre-generated at startup
# ---------------------------------------------------------------------------
STATIC_PROMPTS = {
    "greeting": "Thank you for calling AI BlackBox.",
    # The digit-count prompt is generated per PIN length (ensure_pin_prompt);
    # pin_entry is its length-free fallback.
    "pin_entry": "Please enter your access code.",
    "pin_accepted": "Access granted.",
    "pin_retry": "That code was incorrect. Please try again.",
    "pin_failed": "Too many incorrect attempts. Goodbye.",
    "pin_locked": "Too many incorrect attempts. Please try again later. Goodbye.",
    "pin_unavailable": "This line is not accepting calls right now. Goodbye.",
    "backend_menu": (
        "Select your AI assistant. "
        "Press 1 for Claude. Press 2 for Gemini. "
        "Press 3 for GPT. Press 4 for Grok."
    ),
    "backend_retry": (
        "I didn't catch that. "
        "Press 1 for Claude. Press 2 for Gemini. "
        "Press 3 for GPT. Press 4 for Grok."
    ),
    "backend_invalid": "That's not a valid option. Press 1, 2, 3, or 4.",
    "backend_timeout": "No selection made. Connecting you to the default assistant.",
    "confirm_claude": "Connecting you to Claude. One moment.",
    "confirm_gemini": "Connecting you to Gemini. One moment.",
    "confirm_gpt": "Connecting you to GPT. One moment.",
    "confirm_grok": "Connecting you to Grok. One moment.",
    "goodbye": "Thank you for calling AI BlackBox. Goodbye.",
    "connecting": "One moment while I connect you.",
    "operator_invalid": "That's not a valid selection. Please try again.",
    "operator_timeout": "No selection received. Proceeding as guest caller.",
}


async def ensure_all_prompts():
    """Generate all static IVR prompts if not already cached.

    Called at startup to pre-generate the full set of IVR audio files.
    """
    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
    os.system(f'sudo mkdir -p "{ASTERISK_SOUND_DIR}"')
    os.system(f'sudo chmod 755 "{ASTERISK_SOUND_DIR}"')

    generated = 0
    cached = 0
    failed = 0

    for name, text in STATIC_PROMPTS.items():
        asterisk_path = os.path.join(ASTERISK_SOUND_DIR, f"{name}.wav")
        if os.path.exists(asterisk_path) and os.path.getsize(asterisk_path) > 100:
            cached += 1
            continue
        success = await generate_prompt(name, text)
        if success:
            generated += 1
        else:
            failed += 1

    total = len(STATIC_PROMPTS)
    print(
        f"[IVR-AUDIO] Prompts ready: {cached} cached + {generated} generated"
        f" = {cached + generated}/{total}"
        + (f" ({failed} failed)" if failed else "")
    )

    # Pre-generate the PIN prompt for the configured PIN length
    try:
        from Orchestrator.phone import pin_gate
        if pin_gate.pin_length():
            await ensure_pin_prompt(pin_gate.pin_length())
    except Exception as e:
        print(f"[IVR-AUDIO] PIN prompt generation error: {e}")

    # Pre-generate dynamic operator menu so it's available before first call
    try:
        from Orchestrator.config import USERS_LIST
        if USERS_LIST and len(USERS_LIST) > 1:
            menu_name = await generate_operator_menu(USERS_LIST)
            print(f"[IVR-AUDIO] Operator menu pre-generated: {menu_name}")
    except Exception as e:
        print(f"[IVR-AUDIO] Operator menu generation error: {e}")

    # Reload Asterisk sounds index so newly generated WAVs are found by ARI
    try:
        subprocess.run(
            ['sudo', 'asterisk', '-rx', 'core reload'],
            capture_output=True, timeout=10,
        )
        print("[IVR-AUDIO] Asterisk sounds index reloaded")
    except Exception as e:
        print(f"[IVR-AUDIO] Asterisk reload warning: {e}")
