"""On-box local-model weight downloads from the Hugging Face CDN (M2).

Cloned from the Ollama pull pattern (embeddings/ollama_io.py): a process-wide
single-flight singleton (409 on a concurrent download), NDJSON progress
streamed directly in the POST /local-models/download response, an
httpx.MockTransport test seam, atomic .part -> final rename, re-run-safe skip
when the destination already exists. The disk gate (>=40GB free) lives in the
route (hardware.disk_free_mb, the ONE shared M1 probe) so the module stays
HTTP-only and easy to test.

Weights land under ~/.blackbox/localstack/models (LOCALSTACK_MODELS) — the
same dir the generated llama-swap config's ${models-dir} macro points at.
blackbox.service runs ProtectHome=no, so the Orchestrator can write there.
"""
import json
import os
import threading
from pathlib import Path

import httpx

# Monkeypatchable in tests (MEMINFO_PATH pattern). Resolved once at import;
# blackbox.service runs as REAL_USER so ~ is $REAL_HOME.
MODELS_DIR = Path(os.path.expanduser("~/.blackbox/localstack/models"))

MIN_FREE_GB = 40.0                 # full GPU-tier weight set ~34GB (8B rerank per D13; §7/§14)
CHUNK = 1 << 20                    # 1 MiB progress granularity
DOWNLOAD_TIMEOUT = httpx.Timeout(None, connect=15.0)  # long file, no read cap
# hf_snapshot resilience (2026-07-22 bring-up lessons): abort a hung read so a
# dropped CDN connection can't wedge forever, and retry per repo (re-run-safe).
_HF_SNAPSHOT_READ_TIMEOUT = 30     # seconds; -> HF_HUB_DOWNLOAD_TIMEOUT
_HF_SNAPSHOT_RETRIES = 4           # attempts per repo before surfacing the error
_HF_SNAPSHOT_RETRY_DELAY = 4.0     # seconds between attempts

# Test seam (providers.py / ollama_io _transport pattern): httpx.MockTransport.
_async_transport: "httpx.AsyncBaseTransport | None" = None


def _qwen_tts_model_dir() -> Path:
    """Where the Qwen3-TTS variant checkpoints land — the SAME dir the qwen-tts
    member reads (LocalModels/qwen_tts_server/settings.model_dir): the
    QWEN_TTS_MODEL_DIR override, else <repo>/LocalModels/weights/qwen3-tts.
    blackbox.service sets BLACKBOX_ROOT; fall back to this module's repo root."""
    env = os.environ.get("QWEN_TTS_MODEL_DIR")
    if env:
        return Path(env)
    root = os.environ.get("BLACKBOX_ROOT")
    base = Path(root) if root else Path(__file__).resolve().parents[1]
    return base / "LocalModels" / "weights" / "qwen3-tts"


def _speaches_cache_dir() -> Path:
    """Where the Speaches faster-whisper CT2 checkpoints land — the SAME HF hub
    cache the Speaches member reads on first transcription (A1). Honors an
    explicit SPEACHES_CACHE_DIR override, then HF_HOME (-> <HF_HOME>/hub, the
    huggingface_hub on-disk layout), else a documented default under the
    localstack root (~/.blackbox/localstack/hf-cache/hub) so the wizard download
    button and the Speaches member agree on one location. Sibling of
    _qwen_tts_model_dir(); NEVER _qwen_tts_model_dir() (whisper is not a Qwen
    variant)."""
    env = os.environ.get("SPEACHES_CACHE_DIR")
    if env:
        return Path(env)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return MODELS_DIR.parent / "hf-cache" / "hub"


# Named dest buckets an hf_snapshot manifest entry may target via "dest_dir"
# (A1). Absent/unknown -> the historical default (_qwen_tts_model_dir), keeping
# the bundled qwen-tts key byte-identical. "speaches_cache" pulls whisper into
# the HF hub cache Speaches reads (cache_dir layout, not a per-variant local_dir).
_SPEACHES_CACHE_BUCKET = "speaches_cache"


def _artifact_dest_root(dest_dir: "str | None") -> Path:
    """Resolve an hf_snapshot entry's "dest_dir" bucket to a base Path. Default
    (None / "qwen_tts") preserves today's _qwen_tts_model_dir()."""
    if dest_dir == _SPEACHES_CACHE_BUCKET:
        return _speaches_cache_dir()
    return _qwen_tts_model_dir()


# Two artifact kinds (correction — the GPU-tier weight set is not all single
# GGUFs): "file" = a single HF-CDN GGUF into MODELS_DIR (embeddings); "hf_snapshot"
# = a multi-file HF repo (or set of repos) pulled via huggingface_hub.
# snapshot_download into a per-artifact dest bucket (A1 "dest_dir"): Qwen3-TTS
# variants -> _qwen_tts_model_dir()/<variant>; whisper -> the Speaches HF cache
# (_speaches_cache_dir()). NOT downloaded through this endpoint (documented, not
# gaps):
#   • rerank-qwen3-8b — Qwen3-Reranker-8B @ Q8_0, SELF-CONVERTED from a pinned
#     llama.cpp build (Task 4.4), not a direct download (~8.1GB on disk once
#     converted; the sequential retrieval group per D13 makes the 8B affordable).
#   • embed-qwen3-0.6b — CPU-tier fallback only (not fetched on a GPU box).
#
# "repo_pending_g3": True marks an hf_snapshot whose repo ids are placeholders
# pinned/confirmed on MS02 at the G3/G4 bring-up (Task F1). The status endpoint
# surfaces the flag so the wizard renders a DISABLED button ("pinned during first
# GPU bring-up") instead of a live 404 button. "bundled": True marks the legacy
# all-variants convenience key kept for status back-compat.
DOWNLOAD_MANIFEST: dict[str, dict] = {
    "embed-qwen3-8b": {
        "kind": "file",
        "repo": "Qwen/Qwen3-Embedding-8B-GGUF",
        "filename": "Qwen3-Embedding-8B-Q8_0.gguf",
        "dest": "Qwen3-Embedding-8B-Q8_0.gguf",
        "approx_gb": 8.1,
    },
    "embed-qwen3-0.6b": {
        "kind": "file",
        "repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
        "filename": "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "dest": "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "approx_gb": 0.6,
    },
    # The three Qwen3-TTS 1.7B variant checkpoints (Base/CustomVoice/VoiceDesign,
    # ~4.5GB each ≈ 13.5GB, §14), split per-variant (A2) so the wizard renders
    # one download button per variant. Each is a single multi-file HF repo →
    # snapshot into _qwen_tts_model_dir()/<variant>, matching what the qwen-tts
    # variant manager loads (variant_manager.backend.load(variant, model_dir)).
    # Repo ids pinned to the real HF "12Hz" family (Qwen3-TTS-Tokenizer-12Hz, cf.
    # qwen_tts_server/app.py:97). G3 PASSED on MS02 2026-07-22 (all 3 variants
    # download + synthesize, batch RTF 0.72, sr 24000; eval/results/2026-07-22-g3-
    # tts.json) → repo_pending_g3 CLEARED so the wizard download buttons go live.
    "qwen-tts-base": {
        "kind": "hf_snapshot",
        "label": "Qwen3-TTS 1.7B — Base",
        "repos": {"base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"},
        "dest_dir": "qwen_tts",
        "repo_pending_g3": False,
        "approx_gb": 4.5,
    },
    "qwen-tts-custom-voice": {
        "kind": "hf_snapshot",
        "label": "Qwen3-TTS 1.7B — Custom Voice (3s clone)",
        "repos": {"custom_voice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"},
        "dest_dir": "qwen_tts",
        "repo_pending_g3": False,
        "approx_gb": 4.5,
    },
    "qwen-tts-voice-design": {
        "kind": "hf_snapshot",
        "label": "Qwen3-TTS 1.7B — Voice Design (text-described)",
        "repos": {"voice_design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"},
        "dest_dir": "qwen_tts",
        "repo_pending_g3": False,
        "approx_gb": 4.5,
    },
    # Whisper (Speaches) CT2 checkpoints (from local_stack.ONBOX_STT_{STREAM,BATCH}
    # _MODEL): stream turbo + batch large-v3. Two HF repos → the Speaches HF hub
    # cache (dest_dir "speaches_cache", cache_dir layout) so the wizard's explicit
    # download button pre-fetches what Speaches would otherwise auto-pull
    # invisibly on first transcription. repo_pending_g3 until confirmed at G4.
    "whisper": {
        "kind": "hf_snapshot",
        "label": "Whisper (faster-whisper large-v3 turbo + batch)",
        "repos": {
            "stream": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "batch": "Systran/faster-whisper-large-v3",
        },
        "dest_dir": "speaches_cache",
        "repo_pending_g3": True,
        "approx_gb": 3.0,
    },
    # Legacy bundled all-variants convenience key (D-2) — RETAINED, marked
    # bundled, so existing per-member status rows / callers don't vanish. Not an
    # artifact child (MEMBER_ARTIFACTS lists the per-variant splits instead).
    "qwen-tts": {
        "kind": "hf_snapshot",
        "label": "Qwen3-TTS 1.7B — all variants (bundled)",
        "repos": {
            "custom_voice": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "base":         "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            "voice_design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        },
        "dest_dir": "qwen_tts",
        "repo_pending_g3": False,
        "bundled": True,
        "approx_gb": 13.5,
    },
}

# Per-member artifact children (A4): the manifest keys that render as individual
# download buttons UNDER an audio MEMBER in GET /local-models/status. The bundled
# "qwen-tts" key is intentionally NOT a child — it IS the member id; its children
# are the per-variant splits. Whisper hangs off the speaches (STT) member.
MEMBER_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "qwen-tts": ("qwen-tts-base", "qwen-tts-custom-voice", "qwen-tts-voice-design"),
    "speaches": ("whisper",),
}

# ── download singleton ────────────────────────────────────────────────────
_DL: dict | None = None            # None = idle / never downloaded this process
_DL_LOCK = threading.Lock()


def _record_state(artifact: str) -> None:
    """Persist a terminal-success download-state row (A3). Lazy import of
    local_stack (it imports THIS module's MODELS_DIR/DOWNLOAD_MANIFEST, so a
    top-level import would cycle); fail-soft is already inside
    record_download_state — a bookkeeping-write failure never fails a download."""
    try:
        from Orchestrator import local_stack
        local_stack.record_download_state(artifact)
    except Exception:  # noqa: BLE001 - bookkeeping only; weights are on disk regardless
        pass


def download_status() -> dict | None:
    """Copy of the live download state (consumed by GET /local-models/status);
    None when idle / never downloaded this process."""
    with _DL_LOCK:
        return dict(_DL) if _DL is not None else None


def _set(**fields) -> None:
    with _DL_LOCK:
        if _DL is not None:
            _DL.update(fields)


def _line() -> bytes:
    with _DL_LOCK:
        payload = dict(_DL) if _DL is not None else {}
    return (json.dumps(payload) + "\n").encode()


def _finish() -> None:
    """Guard a generator that died before a terminal state (client disconnect
    / cancellation) — otherwise the singleton is stuck 'running' (permanent
    409 until restart), the same scar ollama_io._log_pull_task_outcome fixes."""
    with _DL_LOCK:
        if _DL is not None and _DL["state"] == "running":
            _DL["state"] = "error"
            _DL["status"] = "interrupted"
            _DL["error"] = "download interrupted"


def start_download(artifact: str):
    """Claim the download singleton and RETURN the NDJSON async generator.

    The claim is synchronous (before the generator runs) so two racing POSTs
    can never double-start. RuntimeError when a download is already running
    (route -> 409); KeyError for an unknown artifact (route validates first).
    """
    global _DL
    if artifact not in DOWNLOAD_MANIFEST:
        raise KeyError(artifact)
    with _DL_LOCK:
        if _DL is not None and _DL["state"] == "running":
            raise RuntimeError(f"a download of {_DL['artifact']!r} is already running")
        _DL = {
            "artifact": artifact, "status": "starting", "completed": 0,
            "total": 0, "state": "running", "error": None,
        }
    return _stream(artifact)


async def _stream(artifact: str):
    """Yield NDJSON progress for one artifact. "file" artifacts stream a single
    HF-CDN GGUF to <dest>.part then atomically rename; "hf_snapshot" artifacts
    pull a multi-file HF repo set via snapshot_download. Terminal line carries
    state 'done' (success or already-present) or 'error'."""
    entry = DOWNLOAD_MANIFEST[artifact]
    if entry.get("kind") == "hf_snapshot":
        async for _l in _stream_hf_snapshot(artifact, entry):
            yield _l
        return
    dest = MODELS_DIR / entry["dest"]
    part = dest.with_name(dest.name + ".part")
    url = (f"https://huggingface.co/{entry['repo']}"
           f"/resolve/main/{entry['filename']}?download=true")
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            size = dest.stat().st_size
            _record_state(artifact)  # persist the download-state contract (A3)
            _set(status="already present", completed=size, total=size, state="done")
            yield _line()
            return
        completed = 0
        total = 0
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, transport=_async_transport, follow_redirects=True
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                _set(status="downloading", completed=0, total=total)
                yield _line()
                with open(part, "wb") as fh:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        fh.write(chunk)
                        completed += len(chunk)
                        _set(status="downloading", completed=completed, total=total)
                        yield _line()
        os.replace(part, dest)
        _record_state(artifact)  # persist the download-state contract (A3)
        _set(status="success", completed=completed, total=total or completed, state="done")
        yield _line()
    except Exception as e:  # network, HTTP, disk — all surface as one error line
        _set(status="error", state="error", error=f"{type(e).__name__}: {e}")
        try:
            part.unlink()
        except OSError:
            pass
        yield _line()
    finally:
        _finish()


async def _stream_hf_snapshot(artifact: str, entry: dict):
    """Pull a multi-file HF repo set via huggingface_hub.snapshot_download into
    this entry's per-artifact dest bucket (A1 "dest_dir"): Qwen3-TTS variants →
    _qwen_tts_model_dir()/<variant> (per-variant local_dir); whisper → the
    Speaches HF hub cache (_speaches_cache_dir(), cache_dir layout so Speaches
    finds it on first use). Coarse progress (completed/total count REPOS, not
    bytes — snapshot_download exposes no byte granularity); re-run-safe
    (snapshot_download skips already-present files). Terminal line state 'done'
    or 'error'. record_download_state fires at terminal success (A3) — multi-file
    artifacts fail _member_gguf_present so the state file is their only truth."""
    import asyncio
    repos = entry["repos"]
    dest_dir = entry.get("dest_dir")
    root = _artifact_dest_root(dest_dir)
    use_cache_layout = dest_dir == _SPEACHES_CACHE_BUCKET
    total = len(repos)
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        _set(status="error", state="error",
             error=f"huggingface_hub unavailable: {e}")
        yield _line()
        _finish()
        return
    # Download robustness (learned on the first on-box bring-up, 2026-07-22): the
    # HF Xet CDN can stall a transfer mid-stream, and a dropped connection with no
    # read timeout hangs forever. Disable Xet, cap the per-read timeout, and use
    # hf_transfer (resilient parallel-chunk pulls) when it is installed. setdefault
    # so an operator env override always wins; snapshot_download reads these per call.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(_HF_SNAPSHOT_READ_TIMEOUT))
    try:
        import hf_transfer  # noqa: F401
        os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    except Exception:
        pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        _set(status="downloading", completed=0, total=total)
        yield _line()
        done_n = 0
        for variant, repo in repos.items():
            _set(status=f"downloading {variant} ({repo})", completed=done_n, total=total)
            yield _line()
            if use_cache_layout:
                # HF hub cache layout (models--org--name) — Speaches reads from
                # here; NOT a flattened per-variant local_dir.
                kwargs = dict(repo_id=repo, cache_dir=str(root))
            else:
                kwargs = dict(repo_id=repo, local_dir=str(root / variant),
                              local_dir_use_symlinks=False)
            # Retry per repo: snapshot_download is re-run-safe (skips complete
            # files), so a retry resumes rather than restarts after a transient
            # network drop. The final failure re-raises to the error line below.
            last_err = None
            for attempt in range(1, _HF_SNAPSHOT_RETRIES + 1):
                try:
                    await asyncio.to_thread(snapshot_download, **kwargs)
                    last_err = None
                    break
                except Exception as ex:  # noqa: BLE001 — surface after retries
                    last_err = ex
                    if attempt < _HF_SNAPSHOT_RETRIES:
                        _set(status=f"retry {variant} {attempt}/{_HF_SNAPSHOT_RETRIES - 1} "
                                    f"({type(ex).__name__})", completed=done_n, total=total)
                        yield _line()
                        await asyncio.sleep(_HF_SNAPSHOT_RETRY_DELAY)
            if last_err is not None:
                raise last_err
            done_n += 1
            _set(status=f"{variant} ready", completed=done_n, total=total)
            yield _line()
        _record_state(artifact)  # persist the download-state contract (A3)
        _set(status="success", completed=total, total=total, state="done")
        yield _line()
    except Exception as e:
        _set(status="error", state="error", error=f"{type(e).__name__}: {e}")
        yield _line()
    finally:
        _finish()
