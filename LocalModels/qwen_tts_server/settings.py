"""Static config + env-driven paths for the qwen-tts server.

STANDALONE: this package MUST NOT import Orchestrator (own lean venv — the MCP
lean-venv lesson). Every cross-process wire (venv, model dir, voices dir, the
G3 streaming flag) arrives via environment variables set on the llama-swap
member's process environment (see README, Task 6.8, for the installer contract).
"""
import os
from pathlib import Path

# The 9 Qwen3-TTS CustomVoice presets (design spec §5.4 / §14 — verified).
PRESET_VOICES = (
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
)

# In-process variant identifiers.
VARIANT_CUSTOM_VOICE = "custom_voice"   # 9 presets — the hot path
VARIANT_BASE = "base"                   # 3-second zero-shot clones
VARIANT_VOICE_DESIGN = "voice_design"   # text-described voices
VARIANTS = (VARIANT_CUSTOM_VOICE, VARIANT_BASE, VARIANT_VOICE_DESIGN)

MIN_CLONE_SECONDS = 3.0   # Base zero-shot cloning reference minimum (§5.4)


def _root() -> Path:
    # BLACKBOX_ROOT set by the unit; else infer the repo root from this file.
    env = os.environ.get("BLACKBOX_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def voices_dir() -> Path:
    env = os.environ.get("QWEN_TTS_VOICES_DIR")
    return Path(env) if env else _root() / "Manifest" / "voices" / "qwen"


def model_dir() -> Path:
    env = os.environ.get("QWEN_TTS_MODEL_DIR")
    return Path(env) if env else _root() / "LocalModels" / "weights" / "qwen3-tts"


def streaming_enabled() -> bool:
    """G3-gated TRUE chunked streaming (§5.4). Default OFF — ships the
    StreamingResponse-over-full-generation fallback (correction [8])."""
    return os.environ.get("QWEN_TTS_STREAMING", "0").strip().lower() in ("1", "true", "yes", "on")


def min_free_vram_mb() -> int:
    """Free-VRAM floor asserted before allocating the next variant (§5.4)."""
    try:
        return int(os.environ.get("QWEN_TTS_MIN_FREE_MB", "5000"))
    except ValueError:
        return 5000


def default_language() -> str:
    """Language passed to the fork's generate_* methods when the request carries
    none. "Auto" is the fork's own default (used in its Base example) and lets the
    model detect the language — multilingual-safe. Override via QWEN_TTS_LANGUAGE."""
    return os.environ.get("QWEN_TTS_LANGUAGE", "Auto").strip() or "Auto"


def attn_implementation() -> str:
    """HF attn kernel for from_pretrained. Default "sdpa" (native, no build) —
    flash-attn is NOT a fork dependency. Set QWEN_TTS_ATTN=flash_attention_2 to try
    FA2 (falls back to sdpa on ImportError). See variant_manager.TorchQwenBackend.load."""
    return os.environ.get("QWEN_TTS_ATTN", "sdpa").strip() or "sdpa"


def stream_emit_frames() -> int:
    """Codec frames per streamed PCM chunk (steady state). 8 frames @ 12Hz ≈ 0.67s."""
    try:
        return int(os.environ.get("QWEN_TTS_STREAM_EMIT_FRAMES", "8"))
    except ValueError:
        return 8


def stream_first_chunk_emit() -> int:
    """Two-phase streaming: emit interval for the FIRST chunk (lower = faster first
    packet). 0 disables two-phase (use the steady interval throughout)."""
    try:
        return int(os.environ.get("QWEN_TTS_STREAM_FIRST_EMIT", "4"))
    except ValueError:
        return 4
