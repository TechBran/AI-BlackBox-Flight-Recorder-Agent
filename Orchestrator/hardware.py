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
        "driver_ok": bool | None,  # additive, see "Driver state" below
        "gpu_reason": str | None,  # additive: why driver_ok is not True
    }

Detection ladder (first hit wins):
  1. nvidia-smi --query-gpu=name,memory.total  → name + exact VRAM
  2. lspci VGA/3D/Display lines mentioning NVIDIA → presence only (vram None;
     the preflight treats unverifiable VRAM as "doesn't fit" — CPU recommended)
  3. neither → no GPU (an AMD iGPU is deliberately NOT a GPU here: the local
     models run through Ollama, whose offload path on our boxes is CUDA)

Driver state (2026-08-11 plan Task 5, audit W11 / O19-local-stack-05): rung 2
used to mean both "the driver is dead" and "the driver was never installed",
and the Aug 7-11 outage hid in that ambiguity (the card stayed on the bus,
Secure Boot rejected the module, and everything kept reporting a GPU box).
  driver_ok True   nvidia-smi answered (rung 1).
  driver_ok False  an NVIDIA card is on the PCI bus and nvidia-smi is
                   installed but failed, hung or listed no GPU: the kernel
                   driver is not serving the card. gpu_reason says what
                   nvidia-smi said.
  driver_ok None   nothing to judge: no NVIDIA card, no NVIDIA userspace on
                   a card's box (never set up for it), or no lspci to see a
                   card with. Consumers fail OPEN on None.
The same table is enforced at unit start by
installer/templates/blackbox-gpu-preflight.sh (Orchestrator/tests/
gpu_driver_cases.py holds the cases both are tested against). derive_tier is
deliberately NOT changed by it (plan D3).

Fail-soft by construction: every command failure (missing binary, non-zero
exit, timeout, garbage output) degrades to the next rung; probe() never
raises. Results are cached for PROBE_TTL_S — status is polled every 2s by the
wizard, and hardware doesn't change under a running service.
"""
import shutil
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


# _run_detailed's reason for a binary that is not on PATH at all.
NOT_INSTALLED = "not installed"


def _run_detailed(cmd: list) -> "tuple[str | None, str | None]":
    """(stdout, None) on a clean exit, else (None, why): NOT_INSTALLED for a
    missing binary, "exit N: <first line it printed>" for a non-zero exit,
    "timed out after Ns" for a hang. Never raises — the probe's fail-soft
    contract lives here, and keeping the reason is what lets probe() tell a
    dead driver from an absent one."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
        )
    except FileNotFoundError:
        return None, NOT_INSTALLED
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_CMD_TIMEOUT_S:g}s"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        said = next((ln.strip() for ln in f"{proc.stderr or ''}\n{proc.stdout or ''}".splitlines()
                     if ln.strip()), "")
        return None, f"exit {proc.returncode}" + (f": {said[:200]}" if said else "")
    return proc.stdout, None


def _run(cmd: list) -> "str | None":
    """stdout of cmd, or None on ANY failure — missing binary, non-zero exit,
    timeout, decode error."""
    return _run_detailed(cmd)[0]


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


def _probe_gpu() -> "tuple[bool, str | None, int | None, str, bool | None, str | None]":
    """(gpu, gpu_name, vram_mb, source, driver_ok, gpu_reason) via the
    nvidia-smi → lspci ladder (driver_ok/gpu_reason: module docstring)."""
    out, smi_err = _run_detailed(_NVIDIA_SMI_CMD)
    if out is not None:
        # First GPU line: "NVIDIA RTX 2000 Ada Generation, 16380". Multi-GPU
        # boxes report GPU 0 — the one Ollama offloads to by default.
        line = next((ln.strip() for ln in out.splitlines() if ln.strip()), None)
        if line:
            name, _, vram_raw = line.rpartition(",")
            try:
                return True, name.strip() or None, int(vram_raw.strip()), "nvidia-smi", True, None
            except ValueError:
                # Unparseable VRAM column — presence is still certain.
                return True, line, None, "nvidia-smi", True, None
        # nvidia-smi ran but listed no GPU — fall through to lspci.
        smi_err = "it listed no GPU"

    out = _run(_LSPCI_CMD)
    if out is not None:
        for ln in out.splitlines():
            low = ln.lower()
            if ("vga" in low or "3d" in low or "display" in low) and "nvidia" in low:
                # "01:00.0 VGA compatible controller: NVIDIA Corporation ..."
                _, _, name = ln.partition(": ")
                name = name.strip() or None
                card = name or "an NVIDIA card"
                if smi_err == NOT_INSTALLED:
                    return True, name, None, "lspci", None, (
                        f"{card} is on the PCI bus but nvidia-smi is not installed "
                        "(no NVIDIA driver on this box), so the on-box models run on CPU")
                return True, name, None, "lspci", False, (
                    f"NVIDIA driver not responding: {card} is on the PCI bus but "
                    f"nvidia-smi failed ({smi_err}). Check nvidia-smi, Secure Boot "
                    "module signing (mokutil --sb-state) and dkms status")

    return False, None, None, "none", None, None


def derive_tier(gpu: bool, vram_mb: "int | None", ram_mb: int) -> str:
    """Hardware tier for reranker/embeddings gating — "LOW" | "MID" | "HIGH".

    HIGH  a GPU with >=8 GB VRAM, OR an lspci-discovered NVIDIA card whose VRAM
          is unverifiable (vram_mb None) — the installer can still target it.
          The 8 GB floor approximately matches (installer vLLM gate >=8000 MB)
          — ~8 GiB (8192 MB) vs 8000 MB, a ~192 MB window: a 6-8 GB card can't
          co-host the Ollama embedder + vLLM, so NOT HIGH.
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

    gpu, gpu_name, vram_mb, source, driver_ok, gpu_reason = _probe_gpu()
    ram_mb = _ram_mb()
    result = {
        "gpu": gpu,
        "gpu_name": gpu_name,
        "vram_mb": vram_mb,
        "ram_mb": ram_mb,
        "source": source,
        "tier": derive_tier(gpu, vram_mb, ram_mb),
        "driver_ok": driver_ok,
        "gpu_reason": gpu_reason,
    }
    with _cache_lock:
        _cache = (now, result)
    return dict(result)


def disk_free_mb(path: "str | None" = None) -> "int | None":
    """Free space (MB) on the filesystem holding `path` (default: the BlackBox
    root). None on ANY failure — fail-soft, never raises. Feeds the local-model
    download gate (~40GB for the full GPU-tier weight set, design §7) and the
    `disk` block of GET /local-models/status. Lazy `paths` import keeps this
    module's import surface minimal (subprocess/shutil only)."""
    try:
        if path is None:
            from Orchestrator.utils.paths import blackbox_root
            path = str(blackbox_root())
        return shutil.disk_usage(path).free // (1024 * 1024)
    except Exception:
        return None
