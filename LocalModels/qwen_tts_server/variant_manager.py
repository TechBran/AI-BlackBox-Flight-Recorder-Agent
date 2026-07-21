"""In-process manager for the three Qwen3-TTS 1.7B variants (§5.4).

ONE process, three variants, exactly ONE resident at a time. FREE-BEFORE-LOAD is
mandatory: drop refs -> gc.collect() -> empty CUDA cache -> VERIFY free VRAM
before allocating the next variant. llama-swap budgets at the PROCESS level and
cannot see an intra-process old+new balloon (~5-6GB each at the mid-swap peak),
so a naive load-then-drop would OOM the 16,380 MiB card. ALL synthesis + swaps
serialize behind one asyncio.Lock so two variants never load concurrently.

The heavy model ops live behind a `backend` object. The default TorchQwenBackend
imports torch + the streaming fork LAZILY (only when a variant is loaded), so
this module imports cleanly and the API/control tests run on a no-GPU box. Tests
inject a fake backend; the real model NEVER loads in CI.
"""
import asyncio
import gc
import logging

from . import settings

log = logging.getLogger("qwen_tts.variant_manager")


class VramError(RuntimeError):
    """Free VRAM is below the safety floor after a free-before-load."""


class VariantManager:
    def __init__(self, backend=None, min_free_mb=None):
        self._backend = backend if backend is not None else TorchQwenBackend()
        self._min_free_mb = settings.min_free_vram_mb() if min_free_mb is None else min_free_mb
        self._current = None    # resident variant name
        self._handle = None     # backend model handle
        self._lock = asyncio.Lock()

    # -- FREE-BEFORE-LOAD -------------------------------------------------
    def _free_before_load(self):
        """Reclaim the resident variant's VRAM BEFORE the next allocation."""
        if self._handle is not None:
            self._backend.free(self._handle)
        self._handle = None
        self._current = None
        gc.collect()
        self._backend.empty_cache()
        free = self._backend.free_vram_mb()
        if free is not None and free < self._min_free_mb:
            raise VramError(
                f"insufficient free VRAM after unload: {free}MB < {self._min_free_mb}MB floor"
            )

    def _ensure_locked(self, variant):
        """Make `variant` resident. Caller MUST hold self._lock."""
        if variant not in settings.VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        if self._current == variant and self._handle is not None:
            return self._handle
        self._free_before_load()               # reclaim old FIRST — never old+new
        self._handle = self._backend.load(variant, settings.model_dir())
        self._current = variant
        log.info("qwen-tts: loaded variant %s", variant)
        return self._handle

    # -- public API -------------------------------------------------------
    async def synthesize_full(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        """Full (non-chunked) generation -> (pcm_s16le_bytes, sample_rate).
        sample_rate is READ FROM THE MODEL OUTPUT (correction [23])."""
        async with self._lock:
            handle = self._ensure_locked(variant)
            return await asyncio.to_thread(
                self._backend.synth, handle, text,
                preset=preset, ref_audio=ref_audio, design_params=design_params,
            )

    async def stream_true(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        """G3-gated TRUE chunked yield. Returns (sample_rate, async_iter[bytes]).
        The lock is held for the FULL stream duration (released in the async
        generator's finally — also on Starlette client-disconnect aclose)."""
        await self._lock.acquire()
        try:
            handle = self._ensure_locked(variant)
            sr = self._backend.sample_rate(handle)
        except BaseException:
            self._lock.release()
            raise

        async def _gen():
            try:
                for chunk in self._backend.synth_stream(
                    handle, text, preset=preset, ref_audio=ref_audio, design_params=design_params
                ):
                    yield chunk
            finally:
                self._lock.release()

        return sr, _gen()

    async def design_preview(self, description, text):
        """VoiceDesign preview -> list[{generated_voice_id, pcm, sr, params}]."""
        async with self._lock:
            handle = self._ensure_locked(settings.VARIANT_VOICE_DESIGN)
            return await asyncio.to_thread(self._backend.design_preview, handle, description, text)

    @property
    def current_variant(self):
        return self._current


def _float_to_pcm16(wavs) -> bytes:
    import numpy as np
    arr = np.asarray(wavs, dtype="float32").reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    return (arr * 32767.0).astype("<i2").tobytes()


class TorchQwenBackend:
    """Real GPU backend. torch + the streaming fork are imported LAZILY inside
    load()/synth()/design_preview() so the module (and CPU-box tests) never need
    CUDA.

    NB: the exact fork call signatures (kunzite-app Qwen3-TTS-streaming
    stream_generate_pcm(), etc.) are CONFIRMED on MS02 in G3 (Task 6.9); the
    shapes below follow the documented fork API. The real model NEVER loads in
    CI — the API/control tests inject a fake backend."""

    def free_vram_mb(self):
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            free, _total = torch.cuda.mem_get_info()
            return free // (1024 * 1024)
        except Exception:
            return None

    def empty_cache(self):
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def load(self, variant, model_dir):
        import torch  # noqa: F401  (ensures CUDA context is up)
        from qwen3_tts_streaming import load_variant  # fork API — confirm in G3
        return load_variant(str(model_dir), variant)

    def free(self, handle):
        try:
            handle.to("cpu")   # release VRAM; the caller drops the ref
        except Exception:
            pass

    def sample_rate(self, handle):
        return int(getattr(handle, "sample_rate", 0)) or None

    def synth(self, handle, text, *, preset=None, ref_audio=None, design_params=None):
        # READ sr FROM THE MODEL OUTPUT — never hardcode 24kHz (correction [23]).
        wavs, sr = handle.generate(text, preset=preset, ref_audio=ref_audio, design=design_params)
        return _float_to_pcm16(wavs), int(sr)

    def synth_stream(self, handle, text, *, preset=None, ref_audio=None, design_params=None):
        # kunzite-app stream_generate_pcm()-style KV-cache streamer (fork).
        # Yields pcm_s16le byte chunks (~3s initial buffer for Base clones, §5.4).
        for pcm_chunk in handle.stream_generate_pcm(
            text, preset=preset, ref_audio=ref_audio, design=design_params
        ):
            yield pcm_chunk

    def design_preview(self, handle, description, text):
        import uuid
        previews = []
        for wavs, sr, params in handle.design_previews(description, text):
            previews.append({
                "generated_voice_id": uuid.uuid4().hex,
                "pcm": _float_to_pcm16(wavs), "sr": int(sr), "params": params,
            })
        return previews
