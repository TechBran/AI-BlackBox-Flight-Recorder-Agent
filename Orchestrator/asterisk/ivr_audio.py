"""
IVR Audio Generator — TTS prompt generation for Asterisk IVR menus.

Generates 8kHz 16-bit mono WAV files from OpenAI TTS-1-HD (onyx voice).
Audio is generated at 24kHz PCM16, then downsampled to 8kHz using a
streaming Butterworth anti-alias filter (same as voice_bridge.py).

Files are saved to:
  - /usr/share/asterisk/sounds/en/custom/ivr/  (Asterisk plays via ARI)
  - Orchestrator/asterisk/ivr_cache/           (local cache; the fallback prompts are tracked)

Asterisk ARI plays prompts as: sound:custom/ivr/{name}  (no .wav extension)

Scripts/install.sh creates the prompt dir owned by the service user and lets
blackbox.service write it (a ReadWritePaths hole in its Asterisk drop-in, as
for /etc/asterisk/blackbox.d), so this module installs and generates prompts
itself: no sudo, and no Asterisk reload (playback reads the file on demand).
The sudo mkdir/cp/chmod and `asterisk -rx 'core reload'` it used to run are
not granted to the service user, and failed silently on every box without a
blanket sudo grant (audit O11-telephony-asterisk-cellular-05). On a box whose
installer predates this, the dir is read-only to the service: the boot log
says so and how to fix it, and calls use the installed prompts and their
fallbacks. The call path never makes or copies audio: it picks an installed
prompt with resolve_prompt().
"""

import asyncio
import hashlib
import os
import tempfile
import traceback
import wave
from typing import List, Optional

import aiohttp
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
# Must equal IVR_SOUND_DIR in Scripts/install.sh (test-pinned).
ASTERISK_SOUND_DIR = "/usr/share/asterisk/sounds/en/custom/ivr"
LOCAL_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ivr_cache")
INSTALL_HINT = ("re-run `sudo bash Scripts/install.sh` (it makes the prompt dir owned by"
                " the service user and writable through blackbox.service's Asterisk"
                " drop-in), then restart blackbox.service")

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


def _usable(path: str) -> bool:
    """True when ``path`` is a WAV big enough to be a real prompt. Never raises."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 100
    except (OSError, ValueError):
        return False


def is_installed(name: str) -> bool:
    """True when prompt ``name`` is installed where Asterisk plays it."""
    return _usable(os.path.join(ASTERISK_SOUND_DIR, f"{name}.wav"))


def prompt_dir_writable() -> bool:
    """True when this process can install prompts into ASTERISK_SOUND_DIR.

    access(2) also reports a read-only mount (EROFS), which is what
    ProtectSystem=strict makes the dir without the installer's drop-in."""
    return os.path.isdir(ASTERISK_SOUND_DIR) and os.access(ASTERISK_SOUND_DIR, os.W_OK)


def _install(local_path: str, asterisk_path: str) -> bool:
    """Install a cached prompt where Asterisk plays it. Never raises.

    Written to a temp file in the dir and renamed over the target, so Asterisk
    never reads a half-written prompt, at 0644 so it can read it. Fails (False)
    when the dir is not writable (see INSTALL_HINT).
    """
    tmp = None
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(asterisk_path),
                                   prefix=".prompt-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
        os.replace(tmp, asterisk_path)
        tmp = None
    except OSError as e:
        print(f"[IVR-AUDIO] Could not install {os.path.basename(asterisk_path)}: {e}")
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return _usable(asterisk_path)


async def _fetch_tts_pcm(text: str, voice: str) -> Optional[bytes]:
    """24 kHz PCM16 for ``text`` from OpenAI TTS, or None."""
    api_key, tts_url = _get_openai_config()
    if not api_key:
        print("[IVR-AUDIO] No OpenAI API key — cannot generate TTS")
        return None

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
                return None
            return await resp.read()


async def generate_prompt(name: str, text: str, voice: str = TTS_VOICE) -> bool:
    """Make sure prompt ``name`` is installed, from the cache or via TTS.

    Returns True only when the prompt is installed where Asterisk plays it.
    A prompt that could only be cached (a read-only sound dir) returns False.
    Never raises (except on cancellation). Not for the call path: TTS can
    take seconds; calls use resolve_prompt().
    """
    asterisk_path = os.path.join(ASTERISK_SOUND_DIR, f"{name}.wav")
    local_path = os.path.join(LOCAL_CACHE_DIR, f"{name}.wav")

    # --- Already in Asterisk sounds? ---
    if _usable(asterisk_path):
        return True

    # --- In local cache? Copy to Asterisk sounds ---
    if _usable(local_path):
        return _install(local_path, asterisk_path)

    # --- Generate via OpenAI TTS ---
    try:
        display_text = text[:60] + ("..." if len(text) > 60 else "")
        print(f'[IVR-AUDIO] Generating: {name} — "{display_text}"')

        pcm_24k = await _fetch_tts_pcm(text, voice)
        if pcm_24k is None:
            return False
        if len(pcm_24k) < 100:
            print(f"[IVR-AUDIO] TTS returned too little data ({len(pcm_24k)} bytes)")
            return False

        # Downsample 24kHz -> 8kHz
        pcm_8k = _resample_24k_to_8k(pcm_24k)
        duration = len(pcm_8k) / (8000 * 2)
        print(f"[IVR-AUDIO] Generated: {name} ({duration:.1f}s, {len(pcm_8k)} bytes)")

        # Save to local cache, then install it if this process can
        os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
        _save_wav(local_path, pcm_8k)
        return _install(local_path, asterisk_path)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[IVR-AUDIO] Error generating {name}: {e}")
        traceback.print_exc()
        return False


def get_prompt_uri(name: str) -> str:
    """Get the Asterisk sound URI for a prompt (used with ARI playMedia).

    Asterisk looks a sound up as <sounds>/<channel language>/<name>, so the
    files in ASTERISK_SOUND_DIR (<sounds>/en/custom/ivr) play as custom/ivr/.
    """
    return f"sound:custom/ivr/{name}"


# Tracked prompts (installed everywhere) that stand in for the optional ones,
# which are only made at boot (ensure_all_prompts) when TTS works.
PROMPT_FALLBACKS = {
    "pin_locked": ("pin_failed",),
    "pin_unavailable": ("goodbye",),
}


def resolve_prompt(name: str, *fallbacks: str) -> str:
    """The first of ``name``, ``fallbacks`` and PROMPT_FALLBACKS that is installed.

    Safe in the call path: an existence check only, never TTS, a copy or an
    exception. With nothing installed it returns ``name`` (Asterisk logs it).
    """
    for candidate in (name, *fallbacks, *PROMPT_FALLBACKS.get(name, ())):
        if is_installed(candidate):
            return candidate
    return name


def pin_prompt_text(length: int) -> str:
    """PIN prompt wording for a ``length``-digit PIN."""
    return f"Please enter your {length}-digit access code."


def pin_prompt_name(length: int) -> str:
    """The installed prompt asking for a ``length``-digit PIN (call path safe).

    pin_prompt_<N>digit carries the length in its name, so a changed PIN
    length never replays a stale "N-digit" recording. Only the 4-digit one is
    tracked; others are made at boot (ensure_all_prompts) when TTS works.
    Falls back to the tracked, length-free ``pin_entry``, then, for 4-digit
    PINs, to the pre-2026-10 ``pin_prompt`` (its recording says "4-digit").
    """
    legacy = ("pin_prompt",) if length == 4 else ()
    return resolve_prompt(f"pin_prompt_{length}digit", "pin_entry", *legacy)


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
    # The digit-count prompt (pin_prompt_<N>digit) is made for the configured
    # PIN length in ensure_all_prompts; pin_entry is its length-free fallback.
    "pin_entry": "Please enter your access code.",
    "pin_prompt_4digit": pin_prompt_text(4),
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
    """Install (or generate) every static IVR prompt that is missing.

    Called at startup. Never raises; on a box where the service cannot write
    the Asterisk sound dir it says once, as an error, how to fix it.
    """
    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
    if not prompt_dir_writable():
        print(
            f"[IVR-AUDIO] ERROR: the IVR prompt dir {ASTERISK_SOUND_DIR} is not writable"
            f" by this service, so missing prompts cannot be installed (callers hear a"
            f" fallback prompt or silence). Fix: {INSTALL_HINT}"
        )

    prompts = dict(STATIC_PROMPTS)
    try:
        from Orchestrator.phone import pin_gate
        if pin_gate.pin_length():
            prompts[f"pin_prompt_{pin_gate.pin_length()}digit"] = pin_prompt_text(pin_gate.pin_length())
    except Exception as e:
        print(f"[IVR-AUDIO] PIN length unavailable: {e}")

    generated = 0
    cached = 0
    not_installed = []   # cached locally, but this process cannot install it
    not_made = []        # no cached copy and TTS failed

    for name, text in prompts.items():
        if is_installed(name):
            cached += 1
            continue
        if await generate_prompt(name, text):
            generated += 1
        elif _usable(os.path.join(LOCAL_CACHE_DIR, f"{name}.wav")):
            not_installed.append(name)
        else:
            not_made.append(name)

    failed = len(not_installed) + len(not_made)
    print(
        f"[IVR-AUDIO] Prompts ready: {cached} cached + {generated} generated"
        f" = {cached + generated}/{len(prompts)}"
        + (f" ({failed} failed)" if failed else "")
    )
    if not_installed:
        print(
            f"[IVR-AUDIO] {len(not_installed)} prompt(s) are cached but not installed in"
            f" {ASTERISK_SOUND_DIR}: {', '.join(not_installed)}. Fix: {INSTALL_HINT}"
        )
    if not_made:
        print(f"[IVR-AUDIO] Could not generate (TTS unavailable): {', '.join(not_made)};"
              f" the IVR plays an installed fallback for them")

    # Pre-generate dynamic operator menu so it's available before first call
    try:
        from Orchestrator.config import USERS_LIST
        if USERS_LIST and len(USERS_LIST) > 1:
            menu_name = await generate_operator_menu(USERS_LIST)
            print(f"[IVR-AUDIO] Operator menu pre-generated: {menu_name}")
    except Exception as e:
        print(f"[IVR-AUDIO] Operator menu generation error: {e}")
    # No Asterisk reload: playback finds a new file on demand.
