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
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

# resolve_cu_model was hoisted to browser/dispatch.py (M1-T1) — it is a pure
# function of the model string + CU_MODEL_FILTERS, shared with the CU dispatch
# path. Imported into this namespace so _execute_cu_job's call site stays local.
from Orchestrator.browser.dispatch import resolve_cu_model
from Orchestrator.provider_billing import CREDITS_EXHAUSTED
from Orchestrator.scheduler.pairing import (
    CRON_PROVIDER_KEYS,
    PROVIDER_WORD_TO_KEY,
    SINGLE_VENDOR_KEYS,
    normalize_provider_word,
)

logger = logging.getLogger(__name__)


def _base_url() -> str:
    """Loopback base URL for the local Orchestrator, derived from config.

    The executor only ever talks to the LOCAL app, so the host stays
    loopback; the port comes from ORCHESTRATOR_PORT (the same canonical port
    config the rest of the app uses) instead of a hardcoded 9091, so a fresh
    box that runs on a different port reaches its own app. config is a leaf
    module, so importing it here is import-cycle-safe; config.orchestrator_url
    is the one helper every in-process loopback caller uses.
    """
    from Orchestrator.config import orchestrator_url

    return orchestrator_url()


# Resolved once at import so the hot path doesn't re-read config per request.
BASE_URL = _base_url()

# ---------------------------------------------------------------------------
# Polling configuration
# ---------------------------------------------------------------------------


def _poll_timeout_from_config() -> int:
    """config.ini [scheduler] poll_timeout_s, default 1800 (30 minutes).

    The budget for one /chat turn, tool calls included. It used to be 180 s,
    shorter than an ordinary web-search or media turn, so the poll gave up on
    turns that then finished (and delivered) anyway (O15-02). Running out of
    it is now 'outcome unknown', never a retry, so a long budget only delays
    the record of a slow turn."""
    from Orchestrator.config import CFG

    try:
        return max(60, CFG.getint("scheduler", "poll_timeout_s", fallback=1800))
    except ValueError:
        logger.warning("config.ini [scheduler] poll_timeout_s is not a number; using 1800")
        return 1800


_POLL_INTERVAL_SECS = 2          # seconds between status checks
_POLL_TIMEOUT_SECS = _poll_timeout_from_config()  # total budget for one turn
_CHAT_REQUEST_TIMEOUT = 30       # timeout for the initial POST to /chat
_TASK_STATUS_TIMEOUT = 10        # timeout for each GET /tasks/status/{id}

# Computer-use streaming configuration
_CU_STREAM_FLOOR = 600           # never wait less than the old 10-minute budget
_CU_CONNECT_TIMEOUT = 30         # Initial connection timeout


def _cu_stream_timeout() -> int:
    """The budget for one CU SSE stream: the configured CU session budget plus
    the worker's 30 s backstop and a margin, never below the old 600 s.

    A fixed 600 s gave up on CU sessions the box allows to run 1800 s; the
    timed-out run was then retried, launching a second desktop session."""
    from Orchestrator import config

    return max(_CU_STREAM_FLOOR, int(config.CU_SESSION_TIMEOUT) + 30 + 90)


# ---------------------------------------------------------------------------
# The in-flight run's /chat task id (O15-03)
#
# The manager persists the task id of a run as soon as /chat returns it, so a
# restart mid-run can name the turn it interrupted. The manager sets this sink
# around each attempt; execute_cron_job reports through note_run_task_id. A
# context variable keeps execute_cron_job's signature (tests and callers stub
# it as fn(job)) and confines the sink to the attempt that set it.
# ---------------------------------------------------------------------------
RUN_TASK_ID_SINK: ContextVar[Optional[Callable[[str], None]]] = ContextVar(
    "cron_run_task_id_sink", default=None
)


def note_run_task_id(task_id: str) -> None:
    """Report the current run's /chat task id to the manager, if it listens."""
    sink = RUN_TASK_ID_SINK.get()
    if sink is None:
        return
    try:
        sink(task_id)
    except Exception:
        logger.exception("Could not record cron task id %s", task_id)


# A connect timeout means nothing reached the server, unlike every other
# timeout (aiohttp >= 3.10; older versions raise the plain timeout).
_CONNECT_TIMEOUT_ERRORS = tuple(
    cls for cls in (getattr(aiohttp, "ConnectionTimeoutError", None),) if cls is not None
)

# CU serialization lock — only one CU job at a time (shared Xvfb display)
_CU_LOCK = None

def _get_cu_lock():
    global _CU_LOCK
    if _CU_LOCK is None:
        _CU_LOCK = asyncio.Lock()
    return _CU_LOCK


class CronToolFailure(RuntimeError):
    """The chat turn completed, but tool calls it made failed and were not recovered.

    Raised by execute_cron_job so the run is recorded honestly instead of as a
    success because the model wrote a reply (W15). ``partial`` is True when some
    other tool call in the turn succeeded. The manager records ``result_text``
    (the reply) alongside the error and does NOT retry: the turn already ran and
    was minted, and a re-run would repeat any side effects that did succeed.
    """

    retryable = False

    def __init__(self, message: str, *, result_text: str, failures: list, partial: bool):
        super().__init__(message)
        self.result_text = result_text
        self.failures = failures
        self.partial = partial

    @property
    def delivery_status(self) -> str:
        return "partial" if self.partial else "error"


class CronCreditsExhausted(RuntimeError):
    """The provider refused the job's turn for billing (W09).

    The message starts with ``credits_exhausted:`` so the run record (the
    history ``error`` column) and the cron failure alert name the kind, and
    ``error_kind`` carries it for code. The box-wide billing push was already
    sent (at most one per provider per hour) where the refusal was classified.
    The manager does not retry it: credits do not come back within the
    backoff, so a retry would only repeat the refusal.
    """

    error_kind = CREDITS_EXHAUSTED

    def __init__(self, message: str):
        super().__init__(f"{CREDITS_EXHAUSTED}: {message}")


class CronOutcomeUnknown(RuntimeError):
    """The job's turn may have run, and delivered, but its end was not seen (O15-02).

    Raised when the /chat task is still running after the poll budget, when the
    POST to /chat times out after reaching the box (the executor calls its own
    event loop, so a timeout there means a busy loop, not a refused request),
    and when a CU stream that had begun times out. The turn goes on running, so
    its SMS or call may still happen: the manager records 'outcome_unknown'
    (manager.OUTCOME_UNKNOWN) with ``task_id`` and never retries, since a retry
    submits a second turn with the same DELIVERY instruction.
    """

    error_kind = "outcome_unknown"
    delivery_status = "outcome_unknown"
    retryable = False

    def __init__(self, message: str, *, task_id: Optional[str] = None):
        super().__init__(f"outcome unknown: {message}")
        self.task_id = task_id


class CronRunCancelled(RuntimeError):
    """The job's /chat task was cancelled (task manager, cancel-all). Not
    retried: cancelling is the operator's decision (manager.RUN_CANCELLED).
    The cancel is cooperative, so the turn may have delivered before it landed;
    the manager records the run as cancelled, not as a failure."""

    error_kind = "cancelled"
    retryable = False


class CronTurnIncomplete(RuntimeError):
    """The job's turn ran but did not end cleanly (O15-02).

    Raised when the /chat task completed with no reply text (a model that ends
    its turn on a tool result, or call_anthropic's tool-iteration cap), when the
    task failed after a tool call that may have had effects (a 529 on the
    provider round after a send_sms), and when a CU run reported an error after
    it had acted on the desktop. Its tools, an SMS or a call among them, may
    have taken effect, so it is not retried, like CronToolFailure: a retry
    submits a second turn with the same DELIVERY instruction. The manager
    records it as a run that ran (a one-shot is paused); ``partial`` is True
    when a tool call that did real work succeeded.
    """

    retryable = False

    def __init__(self, message: str, *, result_text: str = "", partial: bool = False):
        super().__init__(message)
        self.result_text = result_text
        self.partial = partial

    @property
    def delivery_status(self) -> str:
        return "partial" if self.partial else "error"


class CronJobInvalid(RuntimeError):
    """The stored job cannot run as written: its provider is outside cron scope
    (a row stored before the manager refused one). Nothing is submitted, and it
    is not retried, since every attempt would fail the same way."""

    retryable = False


def _summarize_tool_failures(failures: list) -> str:
    """'list_events (x2): <error>; gmail_search: <error>' — grouped by tool, in order.

    A failure that a later call of the same tool with other arguments followed
    successfully (tool_outcomes marks it ``retried``) says so: it may be the
    model correcting itself, or a piece of the job that did not happen.
    """
    grouped: Dict[str, list] = {}
    for f in failures:
        slot = grouped.setdefault(f.get("tool", "?"), [0, "", False])
        slot[0] += 1
        slot[1] = f.get("error") or slot[1]
        slot[2] = slot[2] or bool(f.get("retried"))
    parts = []
    for tool, (count, error, retried) in grouped.items():
        label = f"{tool} (x{count})" if count > 1 else tool
        text = f"{label}: {error}" if error else label
        if retried:
            text += f" (a later {tool} call with other arguments worked)"
        parts.append(text)
    return "; ".join(parts)


def _real_work_calls(outcomes: Any) -> list:
    """The tool calls of a turn that may have had effects: every call in its
    tool_outcomes log except tool discovery and the clock (``incidental``)."""
    if not isinstance(outcomes, list):
        return []
    return [o for o in outcomes
            if isinstance(o, dict) and o.get("tool") and not o.get("incidental")]


def _summarize_tool_calls(calls: list) -> str:
    """'send_sms ok; list_events failed (x2)' — grouped by tool and result, in order."""
    grouped: Dict[tuple, int] = {}
    for o in calls:
        key = (o.get("tool", "?"), bool(o.get("success")))
        grouped[key] = grouped.get(key, 0) + 1
    return "; ".join(
        f"{tool} {'ok' if ok else 'failed'}" + (f" (x{count})" if count > 1 else "")
        for (tool, ok), count in grouped.items()
    )


def _raise_on_tool_failures(result_data: Dict[str, Any], job_name: str, reply: str) -> None:
    """Raise CronToolFailure when the turn's tool calls failed (see tool_outcomes.verdict).

    A server without the tool-outcome log (no "tool_outcomes" key) and a turn
    with no failing calls pass through unchanged.
    """
    from Orchestrator.tool_outcomes import verdict

    found = verdict(result_data.get("tool_outcomes"))
    if found is None:
        return
    failures = found["failures"]
    summary = _summarize_tool_failures(failures)
    if found["partial"]:
        message = f"partial run: {len(failures)} of {found['total']} tool calls failed — {summary}"
    else:
        message = f"tool calls failed — {summary}"
    logger.warning("Cron job '%s' %s", job_name, message)
    raise CronToolFailure(message, result_text=reply, failures=failures, partial=found["partial"])


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
        RuntimeError: If the chat API is unreachable or the task fails.
        CronToolFailure: If the turn completed but tool calls it made failed
            (a RuntimeError subclass carrying the reply; not retried).
        CronOutcomeUnknown: If the turn may have run but its end was not seen
            (poll budget exhausted, POST timed out); not retried.
        CronTurnIncomplete: If the turn ran but did not end cleanly (no reply
            text, or a failure after a tool call or a CU action); not retried.
        CronRunCancelled: If the task was cancelled; not retried.
        CronJobInvalid: If the job's provider is outside cron scope; nothing
            is submitted.
    """
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
    # A provider WORD a raw HTTP client stored before the manager normalized it
    # ('claude', 'grok') is sent as its catalog key: /chat maps only 'gemini',
    # and Auto would resolve the word to the Gemini default (O15-05).
    provider = normalize_provider_word(job.get("provider")) or _model_to_provider(model)
    if provider not in CRON_PROVIDER_KEYS:
        raise CronJobInvalid(
            f"cron job '{job_name}' has provider '{provider}', which cron cannot run "
            f"(one of {', '.join(sorted(CRON_PROVIDER_KEYS))}); edit the job's provider"
        )

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
    except _CONNECT_TIMEOUT_ERRORS as exc:
        # Nothing was sent: an ordinary, retryable failure.
        raise RuntimeError(
            f"Timed out connecting to chat API: {exc or exc.__class__.__name__}"
        ) from exc
    except asyncio.TimeoutError as exc:
        # Checked before ClientError: aiohttp's ServerTimeoutError is both. The
        # request reached this box's own loop, which may still create and run
        # the task once it is free, so this is not a retryable failure.
        raise CronOutcomeUnknown(
            f"the POST to /chat timed out after {_CHAT_REQUEST_TIMEOUT}s; the "
            f"turn may still run. Not retried, so its delivery ({delivery}) is "
            f"not repeated."
        ) from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"Failed to connect to chat API: {exc}") from exc

    logger.debug("Job '%s' queued as task %s", job_name, task_id)
    note_run_task_id(task_id)

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
    error_kind = None
    event_type = None
    # O15-02: once the stream is open the CU run is under way on the desktop;
    # losing the stream after that leaves its outcome unknown, not retryable.
    stream_started = False
    # O15-02: the agent has acted on the desktop (an action event, or step 2
    # beginning, which means step 1's actions ran). An error after that is not
    # retried: a re-run would repeat what the first run already did.
    acted = False
    budget = _cu_stream_timeout()

    cu_lock = _get_cu_lock()
    async with cu_lock:
        try:
            timeout = aiohttp.ClientTimeout(
                total=budget,
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
                    stream_started = True

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

                            elif event_type == "error_detail":
                                if isinstance(data, dict):
                                    error_kind = data.get("kind")

                            elif event_type == "error":
                                error_text = data if isinstance(data, str) else str(data)
                                logger.error("[CU-CRON] Error for '%s': %s", job_name, error_text)
                                break

                            elif event_type in ("cu_action", "cu_bash_output", "cu_file_edit"):
                                acted = True

                            elif event_type == "cu_step":
                                if isinstance(data, dict):
                                    step = data.get("step")
                                    if isinstance(step, int) and step >= 2:
                                        acted = True
                                    logger.debug("[CU-CRON] '%s' step %s/%s",
                                        job_name, data.get("step", "?"), data.get("total", "?"))

                            event_type = None

        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            # aiohttp's ServerTimeoutError is both; isinstance names it a timeout.
            reason = (f"timed out after {budget}s" if isinstance(exc, asyncio.TimeoutError)
                      else f"lost its stream ({exc or exc.__class__.__name__})")
            if stream_started:
                raise CronOutcomeUnknown(
                    f"CU job '{job_name}' {reason} while the run was in progress; "
                    f"not retried, so the desktop task is not run twice."
                ) from exc
            raise RuntimeError(
                f"Failed to connect to CU stream for '{job_name}': {reason}"
            ) from exc

    if error_text and not result_text:
        if error_kind == CREDITS_EXHAUSTED:
            raise CronCreditsExhausted(f"CU job '{job_name}' failed: {error_text}")
        if acted:
            raise CronTurnIncomplete(
                f"CU job '{job_name}' failed after it had acted on the desktop: "
                f"{error_text}. Not retried, so the desktop task is not run twice."
            )
        raise RuntimeError(f"CU job '{job_name}' failed: {error_text}")

    return result_text or "(CU job completed with no response text)"


# ---------------------------------------------------------------------------
# Prompt building — delivery context baked into the message
# ---------------------------------------------------------------------------

# M4: the one fact only THIS layer knows — that the run is unattended.
#
# Layering, deliberately: a tool schema can say what to do ABOUT an unattended run
# (navigate_device's `delivery` param already spells out "scheduled/unattended ->
# notify, because Android silently discards a background launch"), but it cannot
# tell the model that THIS run is one. That is a property of the run, and the cron
# executor is the only thing that knows it. "[Scheduled Task: name]" alone leaves
# the model to infer "scheduled" => "nobody is holding the phone", which is exactly
# the inference that produced the measured 07:30 failure (a "navigation started"
# report while the Fold sat locked in a pocket).
#
# Kept SITUATIONAL, not tool-specific: it states the conditions, never names a tool
# or a parameter, so every device/notification tool benefits and the cron path does
# not acquire a dependency on any one tool's vocabulary.
_UNATTENDED_NOTICE = (
    "This is an UNATTENDED scheduled run — nobody typed this just now and nobody is "
    "watching a screen. The user may be asleep, driving, or have the phone locked in "
    "a pocket, so anything you send to a device must be something that WAITS for "
    "them (a notification they tap later), not something that assumes it can take "
    "over the screen right now. Report what you actually achieved, not what you "
    "intended: something queued for a tap has not happened yet."
)


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

    The header also states that the run is UNATTENDED (see _UNATTENDED_NOTICE) —
    the one piece of context no tool schema can supply for itself.
    """
    header = f"[Scheduled Task: {job_name}]\n{_UNATTENDED_NOTICE}\n\n"

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

    A task still running when the budget runs out raises CronOutcomeUnknown,
    not a retryable error: the turn keeps running and may still deliver, so a
    retry would submit a second turn with the same instructions (O15-02).
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    last_status = ""

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
            last_status = status or last_status

            if status == "completed":
                # Tool failures first: a turn whose tools all failed may still
                # carry (or lack) reply text, and the tool error is the real story.
                result_data = data.get("result_data")
                if isinstance(result_data, dict):
                    _raise_on_tool_failures(result_data, job_name, _reply_text(result_data))
                return _extract_reply(data, job_name, task_id)

            if status == "failed":
                error_msg = data.get("error_message") or ""
                result_data = data.get("result_data") or {}
                detail = error_msg or result_data.get("error", "Unknown error")
                if result_data.get("error_kind") == CREDITS_EXHAUSTED:
                    raise CronCreditsExhausted(
                        f"Chat task {task_id} failed for job '{job_name}': {detail}"
                    )
                # O15-02: a turn that failed after a tool call (a 529 on the
                # provider round after its send_sms) may have delivered; a retry
                # would deliver again. Only a turn with no such call is retried.
                calls = _real_work_calls(result_data.get("tool_outcomes"))
                if calls:
                    raise CronTurnIncomplete(
                        f"Chat task {task_id} failed for job '{job_name}' after its tool "
                        f"calls ran ({_summarize_tool_calls(calls)}): {detail}. Not "
                        f"retried, so those calls are not repeated.",
                        partial=any(o.get("success") for o in calls),
                    )
                raise RuntimeError(
                    f"Chat task {task_id} failed for job '{job_name}': {detail}"
                )

            if status == "cancelled":
                # Terminal (task_routes cancel / cancel-all). Polling on would
                # only run the budget out and report an unknown outcome.
                raise CronRunCancelled(
                    f"Chat task {task_id} for job '{job_name}' was cancelled"
                )

            progress = data.get("progress", 0)
            logger.debug(
                "Task %s for job '%s': status=%s progress=%d%%",
                task_id, job_name, status, progress,
            )

    raise CronOutcomeUnknown(
        f"chat task {task_id} for job '{job_name}' was still "
        f"{last_status or 'unconfirmed'} after {_POLL_TIMEOUT_SECS}s. Not retried, "
        f"so its delivery is not repeated; the turn may still finish and deliver.",
        task_id=task_id,
    )


def _reply_text(result_data: Dict[str, Any]) -> str:
    """The reply text of a completed task's result_data, or "" when it has none."""
    for key in ("ui_reply", "reply", "text"):
        value = result_data.get(key)
        if value and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_reply(task_data: Dict[str, Any], job_name: str,
                   task_id: Optional[str] = None) -> str:
    """Extract the LLM reply text from a completed task.

    A completed task without reply text still RAN (O15-02): a model can end its
    turn on a tool result, and call_anthropic stops at its tool-iteration cap
    with no text, after the send_sms went out. So it raises CronTurnIncomplete
    (not retried), naming the tool calls the turn made.
    """
    task = f"Chat task {task_id}" if task_id else "Chat task"
    result_data = task_data.get("result_data")
    if not result_data or not isinstance(result_data, dict):
        raise CronTurnIncomplete(
            f"{task} for job '{job_name}' completed with no result_data; not "
            f"retried, so whatever the turn did is not repeated."
        )

    reply = _reply_text(result_data)
    if reply:
        return reply

    calls = _real_work_calls(result_data.get("tool_outcomes"))
    ran = f"its tool calls: {_summarize_tool_calls(calls)}" if calls else "it made no tool calls"
    raise CronTurnIncomplete(
        f"{task} for job '{job_name}' completed with no reply text ({ran}); not "
        f"retried, so its delivery is not repeated.",
        partial=any(o.get("success") for o in calls),
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
    from Orchestrator.config import GEMINI_MODEL_DEFAULT, resolve_default_model

    if provider == "custom":
        return _custom_default_model()
    # An empty provider here is the legacy "no provider stored" case, which has
    # always meant Gemini (config's own empty-provider default is DEFAULT_PROVIDER).
    if not provider:
        return GEMINI_MODEL_DEFAULT

    # The same per-provider map every chat path resolves "Auto" through.
    return resolve_default_model(provider)


def _resolve_model_name(model: str, provider: Optional[str] = None) -> str:
    """Resolve a job's stored model to the model id sent to /chat.

    Three cases:
      * Auto — an empty/whitespace model resolves to ``provider``'s configured
        *_MODEL_DEFAULT (M4.1b). The stored provider is authoritative; we no
        longer guess a provider from an empty model string. With no provider
        given it falls back to the Gemini default (legacy behaviour).
      * A bare provider alias ("anthropic"/"gpt"/"grok"/…) resolves to that
        provider's default — unless the job's own provider is a different
        chat provider (W18): then the provider wins and the word means Auto.
      * A specific id ("claude-opus-4-8", "gpt-5.1", …) passes through verbatim.
    """
    m = (model or "").lower().strip()

    # Auto / empty → the provider's configured default at fire time.
    if not m:
        return _provider_default_model((provider or "google").strip().lower())

    word_key = PROVIDER_WORD_TO_KEY.get(m)
    if word_key is None:
        return model

    # W18: the old create default stored "gemini" whatever the provider, and a
    # bare word used to resolve to ITS provider's default regardless, so a
    # provider-anthropic job sent a Gemini id to Anthropic and 404'd every fire.
    # The manager now refuses that pair; rows written before it are healed
    # here. computer-use is left alone (it runs other vendors' models).
    provider_key = normalize_provider_word(provider)
    if provider_key in SINGLE_VENDOR_KEYS + ("custom",) and provider_key != word_key:
        logger.warning(
            "Cron model '%s' belongs to provider %s, not the job's provider %s; "
            "using the %s default", model, word_key, provider_key, provider_key,
        )
        word_key = provider_key
    return _provider_default_model(word_key)
