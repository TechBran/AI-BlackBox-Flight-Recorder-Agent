"""On-box local-model-stack wizard-activation endpoints (M8).

Shares the /local-models/* prefix with M1's status/download router (FastAPI
serves multiple routers under one prefix as long as paths differ).

  POST /local-models/capability   — flip the [local_models] stt|tts seed flag
                                     (+ mirror STT_PROVIDER for stt).
  GET  /local-models/gpu-preflight — nvidia-smi near-idle blocking check the
                                     wizard gates the embeddings cutover on
                                     (Phase-2 Step-0, §10). Fail-open on CPU.

Config writes use a FRESH configparser read-modify-write of config.ini (NOT
the import-time Orchestrator.config.CFG); M1's local_stack resolver reads the
[local_models] section fresh per request, so the flip takes effect with no
restart. config.ini is a gitignored per-box file — never committed.
"""
from __future__ import annotations

import configparser
import logging
import os
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.onboarding.secrets_writer import update_env
from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/local-models", tags=["local-models"])

# Monkeypatched in tests to a tmp config.ini. Resolved once at import; the
# helpers below always read/write THIS path so a test redirect is honored.
CONFIG_INI = resolve("config.ini")

# Only stt/tts activate via the [local_models] seed flag here. embeddings
# activate via POST /embeddings/reembed (the corpus cutover) and rerank via
# POST /rerank/select — each has its own persistence + validation ladder.
_FLAG_CAPS = {"stt", "tts"}

# GPU is "near-idle" enough to safely lazy-load the retrieval group when the
# resident footprint is below this. The pinned pair the Phase-2 reset retires
# is ~10GB (Ollama 8B ~7GB + vLLM ~3.3GB), so a 2GB ceiling reliably catches
# "the old embedder/reranker is still resident" without tripping on the small
# CU/Xvfb llvmpipe footprint (CU renders on CPU, never VRAM).
_GPU_IDLE_CEIL_MIB = 2048


class CapabilityRequest(BaseModel):
    capability: str
    enabled: bool


def _set_local_flag(capability: str, enabled: bool) -> None:
    """Atomic fresh read-modify-write of config.ini [local_models].<cap>."""
    cp = configparser.ConfigParser()
    cp.read(CONFIG_INI)
    if not cp.has_section("local_models"):
        cp.add_section("local_models")
    cp.set("local_models", capability, "true" if enabled else "false")
    tmp = str(CONFIG_INI) + ".tmp"
    with open(tmp, "w") as f:
        cp.write(f)
    os.replace(tmp, CONFIG_INI)


@router.post("/capability")
def set_capability(req: CapabilityRequest) -> dict:
    cap = (req.capability or "").strip().lower()
    if cap not in _FLAG_CAPS:
        raise HTTPException(
            status_code=400,
            detail=(f"capability must be one of {sorted(_FLAG_CAPS)}; "
                    "embeddings activate via /embeddings/reembed and rerank "
                    "via /rerank/select"),
        )
    _set_local_flag(cap, req.enabled)
    # STT routing mirror: the on-box token is the actual resolver preference
    # (resolve_stt_provider). Enable → pin 'onbox'; disable → clear to auto.
    # TTS has no global provider env (the qwen catalog is voice-pick-driven),
    # so the [local_models].tts seed flag above is its whole activation.
    if cap == "stt":
        update_env({"STT_PROVIDER": "onbox" if req.enabled else ""})
    return {"ok": True, "capability": cap, "enabled": req.enabled}


def _probe_gpu_usage() -> dict:
    """Return {present, used_mib, total_mib, processes[]} via nvidia-smi.
    present=False (no GPU / nvidia-smi absent / any error) → callers treat as
    'no contention'. Monkeypatched in tests."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {"present": False, "used_mib": None, "total_mib": None, "processes": []}
        used_s, _, total_s = out.stdout.strip().splitlines()[0].partition(",")
        used_mib, total_mib = int(used_s.strip()), int(total_s.strip())
        procs = []
        papp = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if papp.returncode == 0:
            for line in papp.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    procs.append({"pid": parts[0], "name": parts[1],
                                  "used_mib": int(parts[2]) if parts[2].isdigit() else None})
        return {"present": True, "used_mib": used_mib, "total_mib": total_mib, "processes": procs}
    except Exception:
        logger.info("gpu-preflight: nvidia-smi probe failed (treating as no GPU)", exc_info=True)
        return {"present": False, "used_mib": None, "total_mib": None, "processes": []}


@router.get("/gpu-preflight")
def gpu_preflight() -> dict:
    """Phase-2 Step-0 near-idle precondition (§10). The wizard gates the
    on-box embeddings cutover on ok=true: the retrieval group lazy-loads
    ~11.5-13GB on the first re-embed and would CUDA-OOM if the old pinned
    Ollama 8B / vLLM reranker were still resident. Fail-open on a GPU-less
    box (no VRAM contention is possible)."""
    g = _probe_gpu_usage()
    if not g["present"]:
        return {"ok": True, "present": False, "used_mib": None, "total_mib": None,
                "processes": [], "detail": "No NVIDIA GPU detected — no VRAM contention."}
    ok = (g["used_mib"] or 0) <= _GPU_IDLE_CEIL_MIB
    detail = ("GPU near-idle — safe to load the on-box retrieval group."
              if ok else
              f"GPU holds {g['used_mib']} MiB (ceiling {_GPU_IDLE_CEIL_MIB} MiB). "
              "Free it first — stop the old embedder/reranker "
              "(vllm-reranker.service and the pinned Ollama 8B) — then retry.")
    return {"ok": ok, "present": True, "used_mib": g["used_mib"],
            "total_mib": g["total_mib"], "processes": g["processes"],
            "ceiling_mib": _GPU_IDLE_CEIL_MIB, "detail": detail}
