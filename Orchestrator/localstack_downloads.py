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


# Two artifact kinds (correction — the GPU-tier weight set is not all single
# GGUFs): "file" = a single HF-CDN GGUF into MODELS_DIR (embeddings); "hf_snapshot"
# = a multi-file HF repo pulled via huggingface_hub.snapshot_download (the
# Qwen3-TTS variant checkpoints — ~13.5GB, the bulk of the disk gate — go to
# QWEN_TTS_MODEL_DIR). NOT downloaded through this endpoint (documented, not gaps):
#   • whisper (Speaches) — auto-pulled by the Speaches member on first
#     transcription (its own HF cache); nothing to fetch here.
#   • rerank-qwen3-8b — Qwen3-Reranker-8B @ Q8_0, SELF-CONVERTED from a pinned
#     llama.cpp build (Task 4.4), not a direct download (~8.1GB on disk once
#     converted; the sequential retrieval group per D13 makes the 8B affordable).
#   • embed-qwen3-0.6b — CPU-tier fallback only (not fetched on a GPU box).
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
    # ~4.5GB each ≈ 13.5GB, §14). Multi-file HF repos → snapshot into
    # QWEN_TTS_MODEL_DIR/<variant>, matching what the qwen-tts variant manager
    # loads (variant_manager.backend.load(variant, model_dir)). Exact repo ids are
    # confirmed at G3 (Task 6.9, the same seam that pins the streaming-fork
    # signatures); update here if the open-weights repo names differ.
    "qwen-tts": {
        "kind": "hf_snapshot",
        "repos": {
            "custom_voice": "Qwen/Qwen3-TTS-1.7B-CustomVoice",
            "base":         "Qwen/Qwen3-TTS-1.7B-Base",
            "voice_design": "Qwen/Qwen3-TTS-1.7B-VoiceDesign",
        },
        "approx_gb": 13.5,
    },
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
    """Pull the Qwen3-TTS variant checkpoints (multi-file HF repos) via
    huggingface_hub.snapshot_download into QWEN_TTS_MODEL_DIR/<variant>. Coarse
    progress (completed/total count REPOS, not bytes — snapshot_download exposes
    no byte granularity); re-run-safe (snapshot_download skips already-present
    files). Terminal line state 'done' or 'error'."""
    import asyncio
    repos = entry["repos"]
    root = _qwen_tts_model_dir()
    total = len(repos)
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        _set(status="error", state="error",
             error=f"huggingface_hub unavailable: {e}")
        yield _line()
        _finish()
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        _set(status="downloading", completed=0, total=total)
        yield _line()
        done_n = 0
        for variant, repo in repos.items():
            _set(status=f"downloading {variant} ({repo})", completed=done_n, total=total)
            yield _line()
            await asyncio.to_thread(
                snapshot_download, repo_id=repo,
                local_dir=str(root / variant), local_dir_use_symlinks=False,
            )
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
