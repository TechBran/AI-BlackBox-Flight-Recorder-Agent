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


def _bool_env(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def optimize_enabled() -> bool:
    """A2 (2026-07-22): fork torch.compile optimizations (talker + codebook
    predictor) applied at variant load. DEFAULT OFF — measured on the RTX 2000
    Ada (torch 2.13.0+cu130, G3b eval): compile gives 1.54x on batch-1
    non-streaming synth (RTF 0.47 vs 0.72) but makes the BATCHED hot path
    (A3, the shipped default) WORSE — compiled dynamic-shape kernels ran b=4
    at RTF 0.25-0.27 vs 0.222 eager. Cost: ~33s compile warmup per residency
    (b=1) + one ~21s recompile at the first new batch shape (torch then goes
    dynamic — b=3/b=4 showed no further recompiles). With ttl:600 idle-unload
    and the exclusive retrieval group swapping audio out on every embed,
    default-on would re-pay that warmup constantly to SLOW the batch path.
    Flip QWEN_TTS_OPTIMIZE=1 only for batch-1-heavy deployments (short
    interactive utterances, no long replies)."""
    return _bool_env("QWEN_TTS_OPTIMIZE", "0")


def max_batch() -> int:
    """A3: hard cap on samples per native-batch generate call. Measured VRAM on
    the 16GB card: b=1 4.32GB -> b=4 5.58GB -> b=8 7.37GB (~0.4GB/sample at
    ~250-char texts; 600-char chunks run larger). 8 keeps the co-resident
    whisper member safe; the manager splits larger requests into sub-batches."""
    try:
        return max(1, int(os.environ.get("QWEN_TTS_MAX_BATCH", "8")))
    except ValueError:
        return 8


def batch_vram_mb_per_item() -> int:
    """Per-sample VRAM estimate (MB) for the pre-dispatch batch guard. Measured
    ~400MB/sample at ~250-char texts; default 700 is deliberately conservative
    for 600-char chunks. The guard degrades to smaller sub-batches instead of
    OOMing the co-resident whisper (audit 2026-07-22, VRAM finding)."""
    try:
        return max(100, int(os.environ.get("QWEN_TTS_BATCH_VRAM_MB", "700")))
    except ValueError:
        return 700


def batch_vram_headroom_mb() -> int:
    """Free-VRAM headroom (MB) kept untouched by the batch-size guard."""
    try:
        return max(0, int(os.environ.get("QWEN_TTS_BATCH_HEADROOM_MB", "2048")))
    except ValueError:
        return 2048


# CJK scripts pack far more speech per codepoint than Latin text: one CJK
# character is roughly a syllable (~2-4 audio frames at 12Hz) where Latin runs
# ~5 chars/word. Budgeting CJK at the Latin 2.0 frames/char rate starved real
# Chinese/Japanese/Korean chunks (audit 2026-07-22) — count them at 4.5.
_CJK_RANGES = (
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xAC00, 0xD7A3),   # Hangul Syllables
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def max_new_tokens_for(text: str) -> int:
    """Bound the autoregressive audio-frame budget for ONE synth call, sized to the
    chunk's text length. The model's default is 8192 frames (~11 min at 12Hz); if a
    generation fails to emit an end-of-speech token it runs to that cap, exceeding
    QWEN_TTS_TIMEOUT and 502-ing the whole batch (Brandon 2026-07-22: 'failed due to
    timeout'). Natural 12Hz speech is ~0.8 frames/char (~15 chars/s) for Latin-ish
    text; we allow ~2.5x (QWEN_TTS_FRAMES_PER_CHAR) for pauses/slow voices. SCRIPT-
    AWARE (audit fix): CJK codepoints (CJK Unified/Hiragana/Katakana/Hangul) count
    at QWEN_TTS_FRAMES_PER_CHAR_CJK (4.5) — one CJK char ≈ one syllable, so the
    Latin rate under-budgeted CJK chunks and truncated their audio. A floor covers
    very short text and a hard ceiling backstops runaways. This keeps every chunk
    well within the model's generation budget AND well under the timeout."""
    def _int(name, default):
        try:
            return int(os.environ.get(name, default))
        except ValueError:
            return int(default)

    def _float(name, default):
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return float(default)
    per_char = _float("QWEN_TTS_FRAMES_PER_CHAR", "2.0")
    per_char_cjk = _float("QWEN_TTS_FRAMES_PER_CHAR_CJK", "4.5")
    floor = _int("QWEN_TTS_MIN_NEW_TOKENS", 256)
    ceil = _int("QWEN_TTS_MAX_NEW_TOKENS", 3072)
    t = text or ""
    cjk = sum(1 for ch in t if _is_cjk(ch))
    frames = int((len(t) - cjk) * per_char + cjk * per_char_cjk)
    return max(floor, min(ceil, frames + floor))
