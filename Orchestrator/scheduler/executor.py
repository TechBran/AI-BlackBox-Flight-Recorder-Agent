"""
Cron Job Executor

Thin wrapper that sends scheduled job prompts through the standard /chat
endpoint.  The Orchestrator's existing pipeline handles everything:
context retrieval, embeddings, tool use (phone, SMS, etc.), and auto-mint.

The executor only:
  1. Builds the prompt with delivery context baked in.
  2. Sends ONE request to /chat.
  3. Polls until the task completes.
  4. Returns the result text.
"""

import aiohttp
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

# resolve_cu_model was hoisted to browser/dispatch.py (M1-T1) — it is a pure
# function of the model string + CU_MODEL_FILTERS, shared with the CU dispatch
# path. Imported into this namespace so _execute_cu_job's call site stays local.
from Orchestrator.browser.dispatch import resolve_cu_model

logger = logging.getLogger(__name__)


def _base_url() -> str:
    """Loopback base URL for the local Orchestrator, derived from config.

    The executor only ever talks to the LOCAL app, so the host stays
    loopback; the port comes from ORCHESTRATOR_PORT (the same canonical port
    config the rest of the app uses) instead of a hardcoded 9091, so a fresh
    box that runs on a different port reaches its own app. config is a leaf
    module, so importing it here is import-cycle-safe; falls back to the 9091
    default if config is somehow unavailable.
    """
    try:
        from Orchestrator.config import ORCHESTRATOR_PORT

        return f"http://localhost:{ORCHESTRATOR_PORT}"
    except Exception:
        return "http://localhost:9091"


# Resolved once at import so the hot path doesn't re-read config per request.
BASE_URL = _base_url()

# ---------------------------------------------------------------------------
# Polling configuration
# ---------------------------------------------------------------------------
_POLL_INTERVAL_SECS = 2          # seconds between status checks
_POLL_TIMEOUT_SECS = 180         # total wall-clock budget for a single job
_CHAT_REQUEST_TIMEOUT = 30       # timeout for the initial POST to /chat
_TASK_STATUS_TIMEOUT = 10        # timeout for each GET /tasks/status/{id}

# Computer-use streaming configuration
_CU_STREAM_TIMEOUT = 600         # 10-minute budget for CU SSE stream
_CU_CONNECT_TIMEOUT = 30         # Initial connection timeout

# CU serialization lock — only one CU job at a time (shared Xvfb display)
_CU_LOCK = None

def _get_cu_lock():
    global _CU_LOCK
    if _CU_LOCK is None:
        _CU_LOCK = asyncio.Lock()
    return _CU_LOCK


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_cron_job(job: Dict[str, Any]) -> str:
    """
    Execute a cron job by sending its prompt through the /chat endpoint.

    Delivery instructions (call, SMS, etc.) are embedded in the prompt so
    the LLM handles delivery via its normal tool use.  The /chat pipeline
    takes care of context retrieval, embeddings, and auto-mint.

    Args:
        job: Job dict from the scheduler database.

    Returns:
        The LLM reply text.

    Raises:
        RuntimeError: If the chat API is unreachable, the task fails, or
            the polling timeout is exceeded.
    """
    # Flight Recorder reserved job: direct dispatch to the oversight module —
    # no LLM round-trip to *ask* for a report (design 2026-07-23 §7). The
    # report synthesis itself is the LLM call, inside create_flight_report.
    # The import guard is NARROW and LOUD (review 2026-07-23): a broken
    # oversight import must never silently fall through to the /chat path
    # (which would burn an LLM turn on the placeholder prompt daily).
    _fr_dispatch = None
    try:
        from Orchestrator.oversight import is_reserved_job, create_flight_report_async
        _fr_dispatch = (is_reserved_job, create_flight_report_async)
    except ImportError as e:
        print(f"[FLIGHT-RECORDER] oversight module unavailable in executor: {e}")
    if _fr_dispatch and _fr_dispatch[0](job):
        snap_id = await asyncio.to_thread(_fr_dispatch[1], False)
        return (f"Flight report minted: {snap_id}" if snap_id
                else "Flight report skipped (insufficient activity) or failed — "
                     "see /oversight/status")

    start_time = time.monotonic()
    prompt = job["prompt"]
    model = job.get("model", "gemini")
    operator = job["operator"]
    job_name = job.get("name", "Unnamed Task")
    delivery = job.get("delivery", "snapshot")
    delivery_target = job.get("delivery_target", "") or ""

    # M4.1a/b: the job carries an explicit provider (backfilled from the model
    # for legacy rows by the manager). Trust it as the dispatch provider, and
    # only fall back to deriving from the model string if it is somehow blank.
    provider = (job.get("provider") or "").strip() or _model_to_provider(model)

    # M4.1b: Auto (empty/whitespace model) resolves to THIS provider's
    # configured default at fire time — using the stored provider rather than
    # guessing from an empty model string. A specific id is passed through
    # verbatim.
    resolved_model = _resolve_model_name(model, provider)

    logger.info(
        "Executing cron job '%s' (model=%s, provider=%s, delivery=%s, operator=%s)",
        job_name, resolved_model, provider, delivery, operator,
    )

    # ------------------------------------------------------------------
    # Build the message with delivery context
    # ------------------------------------------------------------------
    content = _build_prompt(job_name, prompt, delivery, delivery_target)

    # CU is streaming-only — route through SSE consumer instead of /chat + polling.
    # M4.1c: thread the resolved CU model through so a chosen CU model is honored
    # (Auto/empty already resolved to CU_MODEL_DEFAULT above).
    if provider == "computer-use":
        return await _execute_cu_job(job_name, content, operator, model=resolved_model)

    payload = {
        "messages": [{"role": "user", "content": content}],
        "provider": provider,
        "model": resolved_model,
        "operator": operator,
    }

    # ------------------------------------------------------------------
    # Submit to /chat (single request — pipeline handles everything)
    # ------------------------------------------------------------------
    task_id: Optional[str] = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BASE_URL}/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_CHAT_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise RuntimeError(
                        f"Chat API returned HTTP {resp.status}: {error[:300]}"
                    )
                data = await resp.json()
                task_id = data.get("task_id")
                if not task_id:
                    raise RuntimeError(
                        f"Chat API did not return a task_id: {data}"
                    )
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"Failed to connect to chat API: {exc}") from exc

    logger.debug("Job '%s' queued as task %s", job_name, task_id)

    # ------------------------------------------------------------------
    # Poll for completion
    # ------------------------------------------------------------------
    result_text = await _poll_task_until_done(task_id, job_name)

    duration = time.monotonic() - start_time
    logger.info(
        "Job '%s' completed: model=%s delivery=%s duration=%.1fs",
        job_name, resolved_model, delivery, duration,
    )

    return result_text


# ---------------------------------------------------------------------------
# Computer-use SSE execution
# ---------------------------------------------------------------------------

async def _execute_cu_job(
    job_name: str,
    content: str,
    operator: str,
    model: Optional[str] = None,
) -> str:
    """Execute a CU cron job by consuming the /chat/stream SSE endpoint.

    CU is streaming-only — it cannot go through /chat (which auto-routes
    CU to plain Anthropic, losing desktop control).
    Passes the original operator so the backend curates full context
    (snapshots, preferences, history) for the CU agent.

    M4.1c: the chosen CU ``model`` is threaded through and honored when it
    passes the capability filters; empty/Auto or an unfilterable id falls back
    to CU_MODEL_DEFAULT (see resolve_cu_model) so a bad id can never break the
    CU streaming path.
    """
    cu_model = resolve_cu_model(model)

    payload = {
        "messages": [{"role": "user", "content": content}],
        "provider": "computer-use",
        "model": cu_model,
        "operator": operator,
    }

    logger.info(
        "Executing CU cron job '%s' via /chat/stream (model=%s, operator=%s)",
        job_name, cu_model, operator,
    )

    result_text = ""
    error_text = ""
    event_type = None

    cu_lock = _get_cu_lock()
    async with cu_lock:
        try:
            timeout = aiohttp.ClientTimeout(
                total=_CU_STREAM_TIMEOUT,
                connect=_CU_CONNECT_TIMEOUT,
            )
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{BASE_URL}/chat/stream",
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise RuntimeError(f"CU stream returned HTTP {resp.status}: {error[:300]}")

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: ") and event_type:
                            data_str = line[6:]
                            try:
                                data = json.loads(data_str)
                                if isinstance(data, str):
                                    try:
                                        data = json.loads(data)
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                            except json.JSONDecodeError:
                                data = data_str

                            if event_type == "done":
                                if isinstance(data, dict):
                                    result_text = data.get("content", "")
                                else:
                                    result_text = str(data)
                                logger.info("[CU-CRON] Done event for '%s'", job_name)
                                break

                            elif event_type == "error":
                                error_text = data if isinstance(data, str) else str(data)
                                logger.error("[CU-CRON] Error for '%s': %s", job_name, error_text)
                                break

                            elif event_type == "cu_step":
                                if isinstance(data, dict):
                                    logger.debug("[CU-CRON] '%s' step %s/%s",
                                        job_name, data.get("step", "?"), data.get("total", "?"))

                            event_type = None

        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Failed to connect to CU stream: {exc}") from exc
        except asyncio.TimeoutError:
            raise RuntimeError(f"CU job '{job_name}' timed out after {_CU_STREAM_TIMEOUT}s")

    if error_text and not result_text:
        raise RuntimeError(f"CU job '{job_name}' failed: {error_text}")

    return result_text or "(CU job completed with no response text)"


# ---------------------------------------------------------------------------
# Prompt building — delivery context baked into the message
# ---------------------------------------------------------------------------

def _build_prompt(
    job_name: str,
    prompt: str,
    delivery: str,
    delivery_target: str,
) -> str:
    """
    Build the full prompt with delivery instructions embedded.

    For snapshot delivery, no extra instructions are needed — auto-mint
    handles it.  For SMS/voice, we tell the LLM to use its tools.
    """
    header = f"[Scheduled Task: {job_name}]\n\n"

    if delivery == "voice_call" and delivery_target:
        return (
            f"{header}{prompt}\n\n"
            f"DELIVERY: After completing the task above, call {delivery_target} "
            f"and deliver your response as a spoken summary."
        )

    if delivery == "sms" and delivery_target:
        return (
            f"{header}{prompt}\n\n"
            f"DELIVERY: After completing the task above, send an SMS to "
            f"{delivery_target} with a concise summary of your response."
        )

    # snapshot or notification — just the prompt, pipeline auto-mints
    return f"{header}{prompt}"


# ---------------------------------------------------------------------------
# Task polling
# ---------------------------------------------------------------------------

async def _poll_task_until_done(task_id: str, job_name: str) -> str:
    """
    Poll /tasks/status/{task_id} until the task reaches a terminal state.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS

    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_SECS)

            try:
                async with session.get(
                    f"{BASE_URL}/tasks/status/{task_id}",
                    timeout=aiohttp.ClientTimeout(total=_TASK_STATUS_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Task status check returned HTTP %d for task %s",
                            resp.status,
                            task_id,
                        )
                        continue

                    data = await resp.json()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Task status poll failed for %s: %s", task_id, exc
                )
                continue

            status = data.get("status", "")

            if status == "completed":
                return _extract_reply(data, job_name)

            if status == "failed":
                error_msg = data.get("error_message") or ""
                result_data = data.get("result_data") or {}
                detail = error_msg or result_data.get("error", "Unknown error")
                raise RuntimeError(
                    f"Chat task {task_id} failed for job '{job_name}': {detail}"
                )

            progress = data.get("progress", 0)
            logger.debug(
                "Task %s for job '%s': status=%s progress=%d%%",
                task_id, job_name, status, progress,
            )

    raise RuntimeError(
        f"Timed out after {_POLL_TIMEOUT_SECS}s waiting for task {task_id} "
        f"(job '{job_name}')"
    )


def _extract_reply(task_data: Dict[str, Any], job_name: str) -> str:
    """Extract the LLM reply text from a completed task."""
    result_data = task_data.get("result_data")
    if not result_data or not isinstance(result_data, dict):
        raise RuntimeError(
            f"Completed task for job '{job_name}' has no result_data"
        )

    for key in ("ui_reply", "reply", "text"):
        value = result_data.get(key)
        if value and isinstance(value, str) and value.strip():
            return value.strip()

    raise RuntimeError(
        f"Completed task for job '{job_name}' has no reply text in result_data"
    )


# ---------------------------------------------------------------------------
# Provider / model mapping
# ---------------------------------------------------------------------------

def _model_to_provider(model: str) -> str:
    """Map a model name to the provider string expected by /chat."""
    m = model.lower()

    # Task 5.2: an alias-qualified custom id ("alias::model") can only come
    # from the custom-server registry — '::' appears in no other provider's
    # ids — so derive "custom" instead of falling through to google (which
    # would run the job on the wrong provider entirely). Checked FIRST: the
    # alias/bare-model halves may contain any vendor substring (e.g.
    # "mybox::computer-use-preview" is still a custom-server model).
    if "::" in m:
        return "custom"
    # M4.1c: CU sub-model ids map to "computer-use" so provider derivation of a
    # CU job stays correct even from the bare model. The "computer-use"
    # substring catches the gemini ("gemini-...computer-use") and openai
    # ("computer-use-preview") CU ids; the bare aliases are the explicit forms.
    # (Anthropic CU uses a normal claude id and is distinguished by the job's
    # stored provider, not the model string.)
    if m in ("computer-use", "cu") or "computer-use" in m:
        return "computer-use"
    if any(tok in m for tok in ("claude", "anthropic", "sonnet", "opus", "haiku")):
        return "anthropic"
    if any(tok in m for tok in ("gpt", "openai", "o1", "o3", "o4")):
        return "openai"
    if any(tok in m for tok in ("grok", "xai")):
        return "xai"

    return "google"


def _custom_default_model() -> str:
    """Registry default for the ``custom`` provider, resolved at fire time.

    Task 5.2: mirrors the /chat/stream + call_custom Auto semantics — the
    FIRST enabled server's first discovered model, alias-qualified via
    custom_servers.qualify. Unlike every other provider there is no static
    config default (the registry is the only source and can change between
    fires), so an unusable registry RAISES RuntimeError with the same
    customer-facing wizard message the chat routes use. The manager's
    _attempt_once catches it and records an error history row (a failed run,
    with the reason) instead of silently running the job on Gemini.
    """
    from Orchestrator.onboarding import custom_servers

    servers = custom_servers.list_servers(enabled_only=True)
    if not servers:
        msg = (
            "cron job failed: provider 'custom' has no resolvable default model — "
            + custom_servers.MSG_NO_SERVERS
        )
        logger.warning(msg)
        raise RuntimeError(msg)

    first = servers[0]
    models = first.get("last_models") or []
    if not models:
        msg = (
            "cron job failed: provider 'custom' has no resolvable default model — "
            + custom_servers.MSG_NO_MODELS.format(alias=first.get("alias", ""))
        )
        logger.warning(msg)
        raise RuntimeError(msg)

    return custom_servers.qualify(first.get("alias", ""), models[0])


def _provider_default_model(provider: str) -> str:
    """Return the configured *_MODEL_DEFAULT for a provider string.

    The single map from a provider to its flagship default, used both for the
    bare-alias aliasing below and for the Auto (empty model) resolution. An
    unknown provider falls back to the Gemini default (same fallthrough the
    legacy alias map used).

    Task 5.2: ``custom`` branches BEFORE the static map — its default is not a
    config constant but a live registry read (see _custom_default_model),
    which raises instead of Gemini-fallthrough when the registry is unusable.
    """
    from Orchestrator.config import (
        CU_MODEL_DEFAULT,
        GEMINI_MODEL_DEFAULT,
        ANTHROPIC_MODEL_DEFAULT,
        OPENAI_MODEL_DEFAULT,
        XAI_MODEL_DEFAULT,
    )

    if provider == "custom":
        return _custom_default_model()

    return {
        "computer-use": CU_MODEL_DEFAULT,
        "anthropic": ANTHROPIC_MODEL_DEFAULT,
        "openai": OPENAI_MODEL_DEFAULT,
        "xai": XAI_MODEL_DEFAULT,
        "google": GEMINI_MODEL_DEFAULT,
    }.get(provider, GEMINI_MODEL_DEFAULT)


def _resolve_model_name(model: str, provider: Optional[str] = None) -> str:
    """Resolve a job's stored model to the model id sent to /chat.

    Three cases:
      * Auto — an empty/whitespace model resolves to ``provider``'s configured
        *_MODEL_DEFAULT (M4.1b). The stored provider is authoritative; we no
        longer guess a provider from an empty model string. With no provider
        given it falls back to the Gemini default (legacy behaviour).
      * A bare provider alias ("anthropic"/"gpt"/"grok"/…) resolves to that
        provider's default.
      * A specific id ("claude-opus-4-8", "gpt-5.1", …) passes through verbatim.
    """
    m = (model or "").lower().strip()

    # Auto / empty → the provider's configured default at fire time.
    if not m:
        return _provider_default_model((provider or "google").strip().lower())

    if m in ("computer-use", "cu"):
        return _provider_default_model("computer-use")
    if m in ("gemini", "google"):
        return _provider_default_model("google")
    if m in ("anthropic", "claude"):
        return _provider_default_model("anthropic")
    if m in ("openai", "gpt"):
        return _provider_default_model("openai")
    if m in ("xai", "grok"):
        return _provider_default_model("xai")
    if m == "custom":
        return _provider_default_model("custom")

    return model
