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

Backend API (kunzite-app/Qwen3-TTS-streaming, pinned commit 80d21b6e — package
`qwen_tts`, class `Qwen3TTSModel`). The wrapper's per-variant methods each return
`(wavs: List[np.ndarray], sr: int)`; the streaming methods yield
`(pcm_chunk: np.ndarray, sr: int)`. The backend is VARIANT-AWARE: the manager
threads the resident variant name into every synth call so the backend dispatches
to the right method (custom_voice/base/voice_design). Sample rate is READ FROM THE
MODEL OUTPUT — never hardcoded (correction [23]).
"""
import asyncio
import gc
import logging

from . import settings

log = logging.getLogger("qwen_tts.variant_manager")


class VramError(RuntimeError):
    """Free VRAM is below the safety floor after a free-before-load."""


def _next_or_none(it):
    """next(it) or None on StopIteration — pulled via asyncio.to_thread so a
    blocking model .step() never stalls the event loop during streaming."""
    try:
        return next(it)
    except StopIteration:
        return None


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

    async def _run_synth_guarded(self, fn, *args, **kwargs):
        """Run a blocking backend synth in a worker thread WITHOUT releasing the
        lock early on cancellation. asyncio.to_thread cancellation abandons the
        thread (it keeps driving CUDA) — if `async with self._lock` then exits,
        the next request can free()/load a variant while the orphaned thread is
        still generating on it (critique 2026-07-22). Caller MUST hold
        self._lock. On cancel we wait for the thread to actually finish before
        re-raising, so the lock is only ever released with the GPU quiet."""
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            try:
                await fut          # drain: thread must exit before lock release
            except Exception:      # noqa: BLE001 — result is discarded anyway
                pass
            raise

    # -- public API -------------------------------------------------------
    async def synthesize_full(self, variant, text, *, preset=None, ref_audio=None,
                              ref_text=None, design_params=None, language=None):
        """Full (non-chunked) generation -> (pcm_s16le_bytes, sample_rate).
        sample_rate is READ FROM THE MODEL OUTPUT (correction [23])."""
        async with self._lock:
            handle = self._ensure_locked(variant)
            return await self._run_synth_guarded(
                self._backend.synth, handle, variant, text,
                preset=preset, ref_audio=ref_audio, ref_text=ref_text,
                design_params=design_params, language=language,
            )

    def _batch_cap(self):
        """Effective per-generate batch cap: the static QWEN_TTS_MAX_BATCH,
        degraded by the free-VRAM guard (audit 2026-07-22: ~0.4GB/sample and
        nothing else checks VRAM before a large batch — the floor check only
        runs at variant load). Uses batch_free_vram_mb (device-free + our own
        caching allocator's reusable pool) when the backend provides it:
        mem_get_info alone counts torch's reserved-but-idle pool as USED, so
        after a few batches a bare device-free reading collapses the cap to 1
        and silently reverts to sequential speed (measured live 2026-07-22).
        Never below 1: b=1 equals today's sequential footprint, which the
        load-time floor already vouched for."""
        cap = settings.max_batch()
        probe = getattr(self._backend, "batch_free_vram_mb", None) or self._backend.free_vram_mb
        free = probe()
        if free is not None:
            fit = int((free - settings.batch_vram_headroom_mb())
                      // settings.batch_vram_mb_per_item())
            if fit < cap:
                log.warning("qwen-tts: batch cap degraded %d -> %d (free VRAM %dMB)",
                            cap, max(1, fit), free)
            cap = min(cap, max(1, fit))
        return cap

    async def synthesize_batch(self, variant, texts, *, preset=None, ref_audio=None,
                               ref_text=None, design_params=None, language=None):
        """A3: native batched generation -> (list[pcm_s16le_bytes], sample_rate).
        ONE lock hold for the whole request; inside it the texts run as
        VRAM-guarded sub-batches of at most _batch_cap() through the fork's
        list-input generate_* (one padded talker.generate per sub-batch,
        per-sample EOS trim — measured 3.3x at b=4, 6.3x at b=8 vs sequential).
        Order of the returned PCMs matches `texts`."""
        texts = [str(t) for t in texts]
        if not texts:
            return [], 0
        async with self._lock:
            handle = self._ensure_locked(variant)
            out, sr = [], 0
            i = 0
            while i < len(texts):
                cap = self._batch_cap()
                sub = texts[i:i + cap]
                pcms, sr = await self._run_synth_guarded(
                    self._backend.synth_batch, handle, variant, sub,
                    preset=preset, ref_audio=ref_audio, ref_text=ref_text,
                    design_params=design_params, language=language,
                )
                out.extend(pcms)
                i += len(sub)
            return out, sr

    async def stream_true(self, variant, text, *, preset=None, ref_audio=None,
                          ref_text=None, design_params=None, language=None):
        """G3-gated TRUE chunked yield. Returns (sample_rate, async_iter[bytes]).
        The sample rate is READ FROM THE FIRST YIELDED CHUNK (the wrapper exposes
        no pre-stream sr attribute) — the first chunk is peeked here, then replayed
        by the generator. The lock is held for the FULL stream duration (released
        in the async generator's finally — also on Starlette client-disconnect
        aclose). Chunks are pulled via asyncio.to_thread so the blocking model
        never stalls the event loop."""
        await self._lock.acquire()
        try:
            handle = self._ensure_locked(variant)
            gen = self._backend.synth_stream(
                handle, variant, text,
                preset=preset, ref_audio=ref_audio, ref_text=ref_text,
                design_params=design_params, language=language,
            )
            first = await asyncio.to_thread(_next_or_none, gen)
        except BaseException:
            self._lock.release()
            raise

        if first is None:
            # Empty generation — nothing to stream; release immediately.
            self._lock.release()

            async def _empty():
                return
                yield  # pragma: no cover  (marks this an async generator)

            return 0, _empty()

        first_pcm, sr = first

        async def _gen():
            try:
                yield first_pcm
                while True:
                    nxt = await asyncio.to_thread(_next_or_none, gen)
                    if nxt is None:
                        break
                    yield nxt[0]
            finally:
                self._lock.release()

        return int(sr), _gen()

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
    """Real GPU backend for the kunzite-app Qwen3-TTS-streaming fork (package
    `qwen_tts`, pinned commit 80d21b6e). torch + the fork are imported LAZILY
    inside load()/synth()/... so the module (and CPU-box tests) never need CUDA.

    The wrapper class is `qwen_tts.Qwen3TTSModel`; `.from_pretrained(dir, ...)`
    returns an instance whose `.model` is the underlying
    Qwen3TTSForConditionalGeneration. Per-variant methods:
      * custom_voice: generate_custom_voice(text, speaker, language, instruct)
      * base:         generate_voice_clone(text, language, ref_audio, ref_text,
                                           x_vector_only_mode, ...)
      * voice_design: generate_voice_design(text, instruct, language)
    each -> (wavs: List[np.ndarray], sr: int). Streaming (custom_voice/base):
    stream_generate_custom_voice / stream_generate_voice_clone -> yield
    (np.ndarray, sr). VoiceDesign has no streaming path -> full-gen fallback.
    The real model NEVER loads in CI — the API/control tests inject a fake backend.
    """

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

    def batch_free_vram_mb(self):
        """VRAM actually available to OUR next batch: device-free (mem_get_info)
        PLUS our caching allocator's reusable pool (reserved - allocated).
        After large padded batches torch retains a multi-GB reserved pool that
        mem_get_info reports as used, but the next generate reuses it freely —
        counting it as busy collapsed the batch cap to 1 within three long
        replies (measured live 2026-07-22: 13GB 'used', RTF back to 0.72)."""
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            free, _total = torch.cuda.mem_get_info()
            reusable = torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
            return (free + max(0, reusable)) // (1024 * 1024)
        except Exception:
            return None

    def load(self, variant, model_dir):
        import torch
        from pathlib import Path
        from qwen_tts import Qwen3TTSModel

        path = str(Path(model_dir) / variant)
        attn = settings.attn_implementation()   # default "sdpa" (flash-attn is not a fork dep)
        try:
            handle = Qwen3TTSModel.from_pretrained(
                path, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation=attn,
            )
        except (ImportError, ValueError, RuntimeError) as exc:
            if attn == "sdpa":
                raise
            log.warning("qwen-tts: attn_implementation=%s failed (%s) -> sdpa", attn, exc)
            handle = Qwen3TTSModel.from_pretrained(
                path, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa",
            )
        if settings.optimize_enabled():
            self._apply_optimizations(handle, variant)
        return handle

    def _apply_optimizations(self, handle, variant):
        """A2 (QWEN_TTS_OPTIMIZE=1): fork torch.compile tiers + warmup synth.
        use_cuda_graphs is ALWAYS off — the fork skips CUDA graphs under
        reduce-overhead anyway (tokenizer_v2 ~:1247), so passing True is a
        no-op that only invites sm_89 instability. use_fast_codebook stays
        False (fork: 'needs debugging, currently slower'). Tiers degrade:
        talker+predictor compile -> predictor-only -> eager (recorded in logs).
        The warmup synth pays the lazy torch.compile cost at variant load
        (~60s measured for b=1 on the 2000 Ada) instead of on the first user
        request; NOTE each NEW batch shape still recompiles (~40-56s), which is
        the core reason optimize defaults OFF now that A3 batches."""
        import time
        tiers = (
            ("talker+predictor", dict(compile_codebook_predictor=True, compile_talker=True)),
            ("predictor-only", dict(compile_codebook_predictor=True, compile_talker=False)),
        )
        for name, kw in tiers:
            try:
                t0 = time.time()
                handle.enable_streaming_optimizations(
                    use_compile=True, use_cuda_graphs=False,
                    use_fast_codebook=False, **kw,
                )
                self._warmup(handle, variant)
                log.info("qwen-tts: optimizations tier=%s enabled in %.1fs (variant=%s)",
                         name, time.time() - t0, variant)
                return
            except Exception as exc:  # noqa: BLE001 — degrade tier-by-tier, never fail the load
                log.warning("qwen-tts: optimization tier=%s failed (%s) — degrading", name, exc)
        log.warning("qwen-tts: all optimization tiers failed — running eager (variant=%s)", variant)

    def _warmup(self, handle, variant):
        """Trigger the lazy torch.compile with a tiny real synth. custom_voice
        and voice_design warm directly; base (clone) needs reference audio we
        don't have at load time — its first real request pays the compile."""
        if variant == settings.VARIANT_CUSTOM_VOICE:
            handle.generate_custom_voice(
                text="Ready.", speaker=settings.PRESET_VOICES[0],
                language=settings.default_language(), instruct=None, max_new_tokens=64,
            )
        elif variant == settings.VARIANT_VOICE_DESIGN:
            handle.generate_voice_design(
                text="Ready.", instruct="", language=settings.default_language(),
                max_new_tokens=64,
            )
        else:
            log.info("qwen-tts: no load-time warmup for variant %s (needs ref audio)", variant)

    def free(self, handle):
        # Qwen3TTSModel wraps the HF module in .model; move it off the GPU so the
        # caller's ref-drop + empty_cache reclaims VRAM (correction: real API has
        # no bare .to on the wrapper).
        try:
            handle.model.to("cpu")
        except Exception:
            try:
                handle.to("cpu")
            except Exception:
                pass

    def sample_rate(self, handle):
        # The wrapper exposes no reliable pre-stream sample-rate attribute; sr is
        # authoritative only from the generate/stream OUTPUT. Return None so
        # stream_true reads it from the first yielded chunk (correction [23]).
        return None

    # -- variant-aware synthesis -----------------------------------------
    def _lang(self, language):
        return language or settings.default_language()

    def synth(self, handle, variant, text, *, preset=None, ref_audio=None,
              ref_text=None, design_params=None, language=None):
        """Dispatch full generation by variant. Returns (pcm_s16le_bytes, sr).
        sr is READ FROM THE MODEL OUTPUT — never hardcoded (correction [23])."""
        lang = self._lang(language)
        # Bound the audio-frame budget to the chunk length so a non-terminating
        # generation can't run away to the model's 8192 default (~11 min) and blow
        # the per-chunk timeout (correction 2026-07-22).
        mnt = settings.max_new_tokens_for(text)
        if variant == settings.VARIANT_CUSTOM_VOICE:
            wavs, sr = handle.generate_custom_voice(
                text=text, speaker=preset, language=lang, instruct=None,
                max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_BASE:
            # No stored transcript -> x-vector-only clone; ref_text present -> ICL.
            x_vector_only = ref_text is None
            wavs, sr = handle.generate_voice_clone(
                text=text, language=lang, ref_audio=ref_audio, ref_text=ref_text,
                x_vector_only_mode=x_vector_only, max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_VOICE_DESIGN:
            wavs, sr = handle.generate_voice_design(
                text=text, instruct=_design_instruct(design_params), language=lang,
                max_new_tokens=mnt,
            )
        else:
            raise ValueError(f"unknown variant {variant!r}")
        return _float_to_pcm16(wavs[0]), int(sr)

    def synth_batch(self, handle, variant, texts, *, preset=None, ref_audio=None,
                    ref_text=None, design_params=None, language=None):
        """A3: ONE padded generate_* call for a list of texts ->
        (list[pcm_s16le_bytes], sr). The fork list-normalizes and broadcasts
        scalar speaker/language/instruct across the text list; the clone path
        broadcasts a SINGLE ref_audio prompt item across all texts (x-vector
        computed once). max_new_tokens is a SINGLE joint-loop ceiling — use the
        max of the per-item budgets (generation still stops per-sample at EOS;
        audit 2026-07-22: batch wall-time = longest sample, ceiling unchanged
        at the per-chunk 3072)."""
        lang = self._lang(language)
        mnt = max(settings.max_new_tokens_for(t) for t in texts)
        if variant == settings.VARIANT_CUSTOM_VOICE:
            wavs, sr = handle.generate_custom_voice(
                text=list(texts), speaker=preset, language=lang, instruct=None,
                max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_BASE:
            x_vector_only = ref_text is None
            wavs, sr = handle.generate_voice_clone(
                text=list(texts), language=lang,
                ref_audio=ref_audio, ref_text=ref_text,
                x_vector_only_mode=x_vector_only, max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_VOICE_DESIGN:
            wavs, sr = handle.generate_voice_design(
                text=list(texts), instruct=_design_instruct(design_params),
                language=lang, max_new_tokens=mnt,
            )
        else:
            raise ValueError(f"unknown variant {variant!r}")
        return [_float_to_pcm16(w) for w in wavs], int(sr)

    def synth_stream(self, handle, variant, text, *, preset=None, ref_audio=None,
                     ref_text=None, design_params=None, language=None):
        """Dispatch TRUE streaming by variant. Yields (pcm_s16le_bytes, sr).
        Two-phase emit (aggressive first chunk) for low first-packet latency.
        VoiceDesign has no streaming path -> single full-gen chunk fallback."""
        lang = self._lang(language)
        emit = settings.stream_emit_frames()
        first_emit = settings.stream_first_chunk_emit()
        mnt = settings.max_new_tokens_for(text)  # bound runaway (see synth)
        if variant == settings.VARIANT_CUSTOM_VOICE:
            gen = handle.stream_generate_custom_voice(
                text=text, speaker=preset, language=lang, instruct=None,
                emit_every_frames=emit, first_chunk_emit_every=first_emit,
                max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_BASE:
            x_vector_only = ref_text is None
            gen = handle.stream_generate_voice_clone(
                text=text, language=lang, ref_audio=ref_audio, ref_text=ref_text,
                x_vector_only_mode=x_vector_only,
                emit_every_frames=emit, first_chunk_emit_every=first_emit,
                max_new_tokens=mnt,
            )
        elif variant == settings.VARIANT_VOICE_DESIGN:
            # No streaming method for VoiceDesign — emit the full generation once.
            pcm, sr = self.synth(
                handle, variant, text, design_params=design_params, language=language,
            )
            yield pcm, sr
            return
        else:
            raise ValueError(f"unknown variant {variant!r}")

        for chunk, sr in gen:
            yield _float_to_pcm16(chunk), int(sr)

    def design_preview(self, handle, description, text):
        """One VoiceDesign preview per call (the real API returns ONE
        (wavs, sr) per generate_voice_design). params carry the instruct so a
        saved profile replays the same designed voice at speak time."""
        import uuid
        wavs, sr = handle.generate_voice_design(
            text=text, instruct=description, language=settings.default_language(),
        )
        return [{
            "generated_voice_id": uuid.uuid4().hex,
            "pcm": _float_to_pcm16(wavs[0]), "sr": int(sr),
            "params": {"instruct": description},
        }]


def _design_instruct(design_params):
    """Extract the natural-language instruct from a saved design profile's params
    (a {'instruct': ...} dict) or accept a bare string; '' means 'no instruction'."""
    if isinstance(design_params, dict):
        return design_params.get("instruct") or ""
    if isinstance(design_params, str):
        return design_params
    return ""
