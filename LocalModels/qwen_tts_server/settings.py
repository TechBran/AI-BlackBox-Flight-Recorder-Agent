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
