"""Host hardware probe (WI-9, retrieval-upgrade M10).

One question, answered fail-soft: what compute does this box have for local
models? Consumed by the embeddings preflight (`_model_preflight` in
routes/embeddings_routes.py) to recommend a GPU/CPU placement per local model,
and surfaced verbatim as the `hardware` block of GET /embeddings/status
(ADDITIVE binding-contract field for the wizard/Portal/Android cards).

    probe() -> {
        "gpu":      bool,        # a usable NVIDIA GPU is present
        "gpu_name": str | None,  # marketing name when known
        "vram_mb":  int | None,  # total VRAM; None when undeterminable (lspci)
        "ram_mb":   int,         # system MemTotal; 0 only if /proc/meminfo fails
        "source":   str,         # "nvidia-smi" | "lspci" | "none"
        "tier":     str,         # "LOW" | "MID" | "HIGH" (derive_tier; additive)
    }

Detection ladder (first hit wins):
  1. nvidia-smi --query-gpu=name,memory.total  → name + exact VRAM
  2. lspci VGA/3D/Display lines mentioning NVIDIA → presence only (vram None;
     the preflight treats unverifiable VRAM as "doesn't fit" — CPU recommended)
  3. neither → no GPU (an AMD iGPU is deliberately NOT a GPU here: the local
     models run through Ollama, whose offload path on our boxes is CUDA)

Fail-soft by construction: every command failure (missing binary, non-zero
exit, timeout, garbage output) degrades to the next rung; probe() never
raises. Results are cached for PROBE_TTL_S — status is polled every 2s by the
wizard, and hardware doesn't change under a running service.
"""
import subprocess
import threading
import time

PROBE_TTL_S = 60.0
_CMD_TIMEOUT_S = 5.0

# nounits keeps memory.total a bare MiB integer ("16380", not "16380 MiB").
_NVIDIA_SMI_CMD = [
    "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits",
]
_LSPCI_CMD = ["lspci"]
MEMINFO_PATH = "/proc/meminfo"  # module attr so tests can point it at a fixture

_cache: "tuple[float, dict] | None" = None
_cache_lock = threading.Lock()


def _run(cmd: list) -> "str | None":
    """stdout of cmd, or None on ANY failure — missing binary, non-zero exit,
    timeout, decode error. The probe's fail-soft contract lives here."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _ram_mb() -> int:
    """System MemTotal in MB from /proc/meminfo; 0 on any failure (a 0 reads
    as 'unknown' downstream — ram_preflight has its own psutil-based check)."""
    try:
        with open(MEMINFO_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except Exception:
        pass
    return 0


def _probe_gpu() -> "tuple[bool, str | None, int | None, str]":
    """(gpu, gpu_name, vram_mb, source) via the nvidia-smi → lspci ladder."""
    out = _run(_NVIDIA_SMI_CMD)
    if out is not None:
        # First GPU line: "NVIDIA RTX 2000 Ada Generation, 16380". Multi-GPU
        # boxes report GPU 0 — the one Ollama offloads to by default.
        line = next((ln.strip() for ln in out.splitlines() if ln.strip()), None)
        if line:
            name, _, vram_raw = line.rpartition(",")
            try:
                return True, name.strip() or None, int(vram_raw.strip()), "nvidia-smi"
            except ValueError:
                # Unparseable VRAM column — presence is still certain.
                return True, line, None, "nvidia-smi"
        # nvidia-smi ran but listed no GPU — fall through to lspci.

    out = _run(_LSPCI_CMD)
    if out is not None:
        for ln in out.splitlines():
            low = ln.lower()
            if ("vga" in low or "3d" in low or "display" in low) and "nvidia" in low:
                # "01:00.0 VGA compatible controller: NVIDIA Corporation ..."
                _, _, name = ln.partition(": ")
                return True, name.strip() or None, None, "lspci"

    return False, None, None, "none"


def derive_tier(gpu: bool, vram_mb: "int | None", ram_mb: int) -> str:
    """Hardware tier for reranker/embeddings gating — "LOW" | "MID" | "HIGH".

    HIGH  a GPU with >=8 GB VRAM, OR an lspci-discovered NVIDIA card whose VRAM
          is unverifiable (vram_mb None) — the installer can still target it.
          The 8 GB floor matches the vLLM installer gate
          (installer/templates/blackbox-install-reranker.sh >=8000 MB): a
          6-8 GB card can't co-host the Ollama embedder + vLLM, so NOT HIGH.
    MID   no GPU but >=32 GB system RAM — opt-in in-process CPU cross-encoder.
    LOW   everything else — cloud-only reranking.
    """
    if gpu and (vram_mb is None or vram_mb >= 8192):
        return "HIGH"
    if not gpu and ram_mb >= 32768:
        return "MID"
    return "LOW"


def probe(ttl_s: float = PROBE_TTL_S) -> dict:
    """Cached host-hardware probe. Never raises; see module docstring for the
    shape. Returns a fresh copy per call — callers can't mutate the cache."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and (now - _cache[0]) < ttl_s:
            return dict(_cache[1])

    gpu, gpu_name, vram_mb, source = _probe_gpu()
    ram_mb = _ram_mb()
    result = {
        "gpu": gpu,
        "gpu_name": gpu_name,
        "vram_mb": vram_mb,
        "ram_mb": ram_mb,
        "source": source,
        "tier": derive_tier(gpu, vram_mb, ram_mb),
    }
    with _cache_lock:
        _cache = (now, result)
    return dict(result)
