"""Ollama daemon I/O — status probes, model pulls, RAM preflight (Task 10).

Sync-vs-async decision (documented per plan): /embeddings/status is a plain
`def` route (FastAPI runs it in the threadpool), so the two tiny daemon GETs
(/api/version, /api/tags) are SYNC httpx.Client calls — no event-loop bridge.
start_pull stays ASYNC: a model pull streams NDJSON for minutes and must live
on the event loop as a background task (migrate.py's singleton/job pattern,
simpler — no persistence; a pull interrupted by restart just gets re-clicked,
and Ollama resumes partial downloads server-side anyway).

Pull progress (`completed`/`total`) reflects the LARGEST layer seen, not the
sum across layers: the model blob dominates an Ollama download, and summing
makes the progress bar jump backwards every time a new layer header arrives.

`binary_installed` and `daemon_version` are deliberately separate signals:
the daemon can be reachable with no binary on PATH (container install) and
the binary present with the daemon stopped.
"""
import asyncio
import json
import shutil
import threading

import httpx
import psutil

from Orchestrator import config

GET_TIMEOUT = httpx.Timeout(2.0, connect=2.0)   # status probes: fail fast
PULL_TIMEOUT = httpx.Timeout(None, connect=5.0)  # pulls are long: no read timeout

# Bytes per declared GB: 1 GB + 7% margin (≈ 1 GiB) — registry ram_gb is a
# model-size declaration, not an exact byte count, so a small headroom factor
# beats false positives.
RAM_BYTES_PER_GB = 1.07e9

# Test seams (providers.py `_transport` pattern): httpx.MockTransport here.
_transport: "httpx.BaseTransport | None" = None             # sync GETs
_async_transport: "httpx.AsyncBaseTransport | None" = None  # pull stream

# ── pull state singleton ─────────────────────────────────────────────────────

_PULL: dict | None = None         # None = idle / never pulled this process
_PULL_LOCK = threading.Lock()     # guards _PULL mutation + reads (copy out)
# Strong ref to the streaming task — the loop holds only WEAK task refs, and a
# garbage-collected task dies silently leaving _PULL stuck "running"
# (permanent 409 until restart). Same scar as migrate._JOB_TASK.
_PULL_TASK: "asyncio.Task | None" = None


# ── daemon probes (sync — see module docstring) ──────────────────────────────

def binary_installed() -> bool:
    """`ollama` binary on PATH (independent of daemon reachability)."""
    return shutil.which("ollama") is not None


def daemon_version() -> str | None:
    """Daemon version string; None when unreachable — the 'running' signal."""
    try:
        with httpx.Client(timeout=GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{config.OLLAMA_BASE_URL}/api/version")
            resp.raise_for_status()
            return str(resp.json().get("version", "unknown"))
    except Exception:
        return None


def local_models() -> list[str]:
    """Names of locally pulled models; [] when unreachable or malformed."""
    try:
        with httpx.Client(timeout=GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{config.OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [m["name"] for m in models if isinstance(m, dict) and "name" in m]
    except Exception:
        return []


# ── RAM preflight ────────────────────────────────────────────────────────────

def ram_preflight(ram_gb: float) -> str | None:
    """Remediation string when free RAM can't fit the model; None when fine
    (always None for cloud models — their registry ram_gb is 0)."""
    if ram_gb <= 0:
        return None
    if psutil.virtual_memory().available < ram_gb * RAM_BYTES_PER_GB:
        return f"Needs ~{ram_gb:g}GB free RAM; close apps or pick the lighter model"
    return None


# ── model pull (async background task) ───────────────────────────────────────

def pull_status() -> dict | None:
    """Copy of the live pull state (the status route's `ollama.pull` field);
    None when idle / never pulled this process."""
    with _PULL_LOCK:
        return dict(_PULL) if _PULL is not None else None


def _update_pull(**fields) -> None:
    with _PULL_LOCK:
        if _PULL is not None:
            _PULL.update(fields)


def _log_pull_task_outcome(task: "asyncio.Task") -> None:
    """Done-callback: surface a death the stream's own exception handling never
    saw (would otherwise be a silent 'running'-forever state)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(
            f"[OLLAMA] ERROR: pull task died with unretrieved exception: "
            f"{type(exc).__name__}: {exc}"
        )
        _update_pull(state="error", error=f"{type(exc).__name__}: {exc}")


async def start_pull(model: str) -> dict:
    """Claim the pull singleton, stream POST /api/pull in the background.

    `model` is a RAW ollama model id (e.g. "qwen3-embedding:0.6b") — the route
    resolves registry slugs before calling. RuntimeError when a pull is
    already running (route maps it to 409). Returns the freshly-claimed pull
    state (state == "running"); the claim happens synchronously BEFORE
    create_task, so two racing calls can never double-start.
    """
    global _PULL, _PULL_TASK
    with _PULL_LOCK:
        if _PULL is not None and _PULL["state"] == "running":
            raise RuntimeError(f"a pull of {_PULL['model']!r} is already running")
        _PULL = {
            "model": model,
            "status": "starting",
            "completed": 0,
            "total": 0,
            "state": "running",
            "error": None,
        }
    task = asyncio.get_running_loop().create_task(_stream_pull(model))
    task.add_done_callback(_log_pull_task_outcome)
    _PULL_TASK = task
    return pull_status()


async def _stream_pull(model: str) -> None:
    """Consume the NDJSON pull stream, updating the singleton as lines arrive.

    Terminal transitions: {"error": ...} line or transport failure → "error";
    {"status": "success"} → "done"; stream ending without a success line is
    treated as an error (daemon died mid-pull)."""
    try:
        async with httpx.AsyncClient(
            timeout=PULL_TIMEOUT, transport=_async_transport
        ) as client:
            async with client.stream(
                "POST",
                f"{config.OLLAMA_BASE_URL}/api/pull",
                json={"name": model, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a torn/garbage line
                    if not isinstance(msg, dict):
                        continue
                    if "error" in msg:
                        _update_pull(
                            state="error", status="error", error=str(msg["error"])
                        )
                        print(f"[OLLAMA] pull {model} failed: {msg['error']}")
                        return
                    status = msg.get("status", "")
                    fields = {"status": status}
                    total = msg.get("total")
                    completed = msg.get("completed")
                    if isinstance(total, int):
                        # Largest-layer progress (module docstring): a smaller
                        # layer's lines update `status` only, never the numbers.
                        with _PULL_LOCK:
                            track = _PULL is not None and total >= _PULL["total"]
                        if track:
                            fields["total"] = total
                            fields["completed"] = (
                                completed if isinstance(completed, int) else 0
                            )
                    _update_pull(**fields)
                    if status == "success":
                        _update_pull(state="done")
                        print(f"[OLLAMA] pull {model} complete")
                        return
        _update_pull(
            state="error", error="pull stream ended without a success line"
        )
        print(f"[OLLAMA] pull {model}: stream ended without a success line")
    except Exception as e:  # connect refused, read drop, HTTP error — all → error
        _update_pull(state="error", error=f"{type(e).__name__}: {e}")
        print(f"[OLLAMA] pull {model} failed: {type(e).__name__}: {e}")
