"""G2-T9 (M2.1) — headless CLI-agent runner.

Makes the installed coding CLIs (`claude`, `gemini`, `codex`) runnable as
background tasks. T10 wraps these as ToolVault tools reachable from chat and the
three voice agents, spawned YOLO fully open. This module owns exactly four
things and no more (T11 owns progress_text; T10 owns the tools):

  1. build_argv        — per-provider argv, every flag verified from --help.
  2. build_child_env   — a REAL per-provider environment strip so an agent uses
                         the operator's *subscription* auth, never a leaked
                         server-side API key that silently bills.
  3. _resolve_bin      — absolute path to the CLI (systemd PATH omits nvm dirs).
  4. run_cli_agent     — spawn (own process group), read stdout JSONL, keep a
                         bounded poll-visible tail, honor cancel + timeout via a
                         process-GROUP kill so the agent's children die too.

Auth routing (why the strip matters — verified on this box 2026-07-10):
  * claude reads ANTHROPIC_API_KEY; if the server's key leaks into the child it
    triggers claude's "both a token and an API key are set" ambiguity and can
    bill the key instead of the cached OAuth token. Strip it.
  * gemini reads GOOGLE_API_KEY / GEMINI_API_KEY; with either present it bills
    the API key instead of the operator's OAuth (Code Assist) login. Strip both.
    GOOGLE_APPLICATION_CREDENTIALS is deliberately KEPT — it is a distinct
    Vertex/ADC service-account path, not the pay-per-call key we are avoiding,
    and the zellij denylist keeps it for the same reason.
  * codex is logged in via ChatGPT (`~/.codex/auth.json` auth_mode=chatgpt), but
    `codex doctor` proves a live OPENAI_API_KEY in the env flips HTTP reachability
    to "API key auth" — overriding the ChatGPT subscription and billing the key.
    Strip OPENAI_API_KEY; the ChatGPT OAuth tokens live in auth.json (not the
    env) and are untouched.

Cancel contract (pairs with G2-T8 in tasks.py):
  * Spawn with start_new_session=True → the child is its own process-group
    leader (pgid == pid). cancel_task does os.killpg(pgid, SIGTERM/SIGKILL),
    which is what reaps the agent's CHILDREN too, not just the launcher.
  * Register the "process" cancel handle (with the real pid) immediately after
    spawn. On natural exit, UNREGISTER the handle BEFORE reaping the child, so a
    late cancel can never killpg a reused pid (the registry contract at
    tasks.py:116).
"""
import collections
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Optional


# --- Provider tables --------------------------------------------------------
# Binary filename per provider (all three happen to match the provider id).
_PROVIDER_BINARY_NAMES = {
    "claude": "claude",
    "gemini": "gemini",
    "codex": "codex",
}

# Model CLASS -> passed verbatim to the CLI's OWN --model flag. The CLI is the
# resolver: we name a *class*, it picks the newest concrete version. We never
# pin a version here (that would break subscription routing and violate the
# "never name a version" rule). Validated against this set in build_argv.
MODEL_CLASSES: dict[str, tuple[str, ...]] = {
    "claude": ("fable", "opus", "sonnet", "haiku"),
    "gemini": ("pro", "flash"),
    "codex": ("gpt", "gpt-sol"),
}

# Per-provider env strip. Each provider strips ONLY the key(s) IT reads and would
# bill on — minimal blast radius (mirrors the zellij denylist philosophy).
_ENV_STRIP: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
}
# NOTE: GOOGLE_APPLICATION_CREDENTIALS is intentionally NOT in gemini's list —
# see the module docstring (Vertex/ADC, kept in lock-step with the zellij denylist).

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(_PROVIDER_BINARY_NAMES.keys())

# Tail / result bounds. Raw agent stdout is untrusted and unbounded; every path
# that surfaces it (poll-visible tail, returned result_text, and — downstream —
# the immutable ledger) must clamp it. T11 will replace `tail` with a nicer
# progress_text; do NOT build that here.
TAIL_MAX_CHARS = 4000
TAIL_MAX_LINES = 400
RESULT_TEXT_MAX_CHARS = 8000

_TAIL_FLUSH_SECONDS = 2.0   # roughly how often the DB tail is refreshed
_POLL_INTERVAL = 0.2
_DEFAULT_TIMEOUT = 1800.0   # 30 min; a coding agent can legitimately run long

# Keys we scan JSONL events for, in priority order, to lift a human-readable
# final message. Best-effort + provider-agnostic on purpose — precise per-provider
# event schemas are T11's problem, not the runner's.
_TEXT_KEYS = ("result", "text", "content", "message", "summary", "delta")


def _resolve_bin(provider: str) -> str:
    """Absolute path to the provider CLI. systemd's PATH omits ~/.local/bin and
    nvm dirs, so search an extended PATH (same list the bridge uses). Falls back
    to the bare name (which fails loudly at spawn if truly missing)."""
    from .path_extension import extended_path_dirs
    bin_name = _PROVIDER_BINARY_NAMES.get(provider, provider)
    extended_path = os.pathsep.join([os.environ.get("PATH", ""), *extended_path_dirs()])
    found = shutil.which(bin_name, path=extended_path)
    return found or bin_name


def build_argv(provider: str, prompt: str, model_class: str, cwd: str,
               permission_mode: str = "yolo", bin_path: Optional[str] = None) -> list[str]:
    """Build the exact argv for a headless, non-interactive agent run.

    permission_mode == "yolo" adds the provider's fully-open flag (the mode T10
    launches in). Any other value = default (no dangerous flag added).
    All flags verified from `--help` on 2026-07-10.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported CLI-agent provider: {provider!r} "
                         f"(supported: {', '.join(SUPPORTED_PROVIDERS)})")
    allowed = MODEL_CLASSES[provider]
    if model_class not in allowed:
        raise ValueError(f"Unknown model class {model_class!r} for {provider!r} "
                         f"(allowed: {', '.join(allowed)}). We pass a class, never "
                         f"a version — the CLI resolves the newest.")
    yolo = (permission_mode == "yolo")
    binp = bin_path or _resolve_bin(provider)

    if provider == "claude":
        # claude -p --output-format stream-json --verbose (--verbose REQUIRED for
        # stream-json) --model <class> [--dangerously-skip-permissions] <prompt>
        argv = [binp, "-p", "--output-format", "stream-json", "--verbose",
                "--model", model_class]
        if yolo:
            argv.append("--dangerously-skip-permissions")
        argv.append(prompt)
        return argv

    if provider == "gemini":
        # gemini --output-format stream-json --model <class>
        #        [--approval-mode yolo] -p <prompt>
        argv = [binp, "--output-format", "stream-json", "--model", model_class]
        if yolo:
            argv.extend(["--approval-mode", "yolo"])
        argv.extend(["-p", prompt])
        return argv

    # codex exec --json --skip-git-repo-check -C <cwd> --model <class>
    #            [--dangerously-bypass-approvals-and-sandbox] <prompt>
    argv = [binp, "exec", "--json", "--skip-git-repo-check", "-C", cwd,
            "--model", model_class]
    if yolo:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    argv.append(prompt)
    return argv


def build_child_env(provider: str, base_env: Optional[dict] = None) -> dict:
    """Return the environment for the spawned CLI: the base env with the
    provider's billing keys stripped and the PATH augmented so an nvm-installed
    CLI's `#!/usr/bin/env node` shebang resolves.

    The strip is the whole point of the feature — see the module docstring.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported CLI-agent provider: {provider!r}")
    env = dict(os.environ if base_env is None else base_env)

    for key in _ENV_STRIP.get(provider, ()):  # <-- the environment strip
        env.pop(key, None)

    from .path_extension import extended_path_dirs
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), *extended_path_dirs()])
    return env


def _extract_result_text(events: list) -> str:
    """Best-effort final human-readable text from parsed JSONL events. Scans for
    the last text-bearing field. Provider-agnostic; bounded by the caller."""
    text = ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for k in _TEXT_KEYS:
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                text = v
                break
    return text


def _read_stream(stream, raw_lines: collections.deque, events: collections.deque,
                 lock: threading.Lock) -> None:
    """Reader thread: pull lines off the child's stdout until EOF (blocking
    iteration). EOF arrives naturally on exit, and immediately when a group kill
    closes the pipe, so this thread always terminates."""
    try:
        for raw in stream:
            line = raw.rstrip("\n")
            with lock:
                raw_lines.append(line)
                try:
                    events.append(json.loads(line))
                except (ValueError, TypeError):
                    pass  # non-JSON banner/log line — kept in raw tail only
    except (ValueError, OSError):
        pass  # pipe closed under us (kill) — expected


def run_cli_agent(provider: str, prompt: str, model_class: str, cwd: str,
                  permission_mode: str = "yolo", task_id: Optional[str] = None,
                  timeout: float = _DEFAULT_TIMEOUT,
                  bin_path: Optional[str] = None) -> dict:
    """Spawn a headless CLI agent, stream its JSONL stdout, and return a result
    dict. Registers a T8 "process" cancel handle so /tasks/{id}/cancel does a
    process-group kill. Completion = process exit + drained stdout.

    Returns: {success, exit_code, cancelled, timed_out, result_text, tail,
              events, provider}.
    """
    # Lazy import to avoid a tasks.py <-> headless.py import cycle.
    from Orchestrator.tasks import (register_cancel_handle, unregister_cancel_handle,
                                    is_cancel_requested, update_task, _kill_process_group)

    argv = build_argv(provider, prompt, model_class, cwd, permission_mode, bin_path)
    env = build_child_env(provider)

    print(f"[CLI-AGENT] spawn provider={provider} model_class={model_class} "
          f"cwd={cwd} mode={permission_mode} task={task_id}")

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # fold stderr into the JSONL stream/tail
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,     # OWN process group — see cancel contract
    )

    # Register the cancel handle with the real pid BEFORE we start waiting, so a
    # cancel that lands mid-run finds a "process" handle and group-kills it.
    if task_id:
        register_cancel_handle(task_id, "process", pid=proc.pid,
                               provider=provider)

    raw_lines: collections.deque = collections.deque(maxlen=TAIL_MAX_LINES)
    events: collections.deque = collections.deque(maxlen=TAIL_MAX_LINES)
    lock = threading.Lock()
    reader = threading.Thread(target=_read_stream,
                              args=(proc.stdout, raw_lines, events, lock),
                              daemon=True)
    reader.start()

    def _current_tail() -> str:
        with lock:
            joined = "\n".join(raw_lines)
        return joined[-TAIL_MAX_CHARS:]

    deadline = time.monotonic() + timeout
    last_flush = 0.0
    cancelled = False
    timed_out = False

    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if task_id and is_cancel_requested(task_id):
            cancelled = True
            _kill_process_group(proc.pid, task_id or "")
            break
        if time.monotonic() > deadline:
            timed_out = True
            _kill_process_group(proc.pid, task_id or "")
            break
        now = time.monotonic()
        if task_id and now - last_flush >= _TAIL_FLUSH_SECONDS:
            last_flush = now
            update_task(task_id, result_data={
                **(_result_data_for(task_id, provider, model_class)),
                "tail": _current_tail(),
            })
        time.sleep(_POLL_INTERVAL)

    # Drain the reader (the pipe is at EOF after exit, or closed by the kill).
    reader.join(timeout=5)

    # UNREGISTER the process handle BEFORE reaping, so a late cancel can never
    # killpg a reused pid (registry contract). Idempotent with process_task's
    # finally, which also unregisters.
    if task_id:
        unregister_cancel_handle(task_id)

    try:
        exit_code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        exit_code = proc.poll()

    tail = _current_tail()
    with lock:
        ev_list = list(events)
    result_text = _extract_result_text(ev_list)
    if not result_text:
        result_text = tail
    result_text = result_text[:RESULT_TEXT_MAX_CHARS]

    success = (exit_code == 0) and not cancelled and not timed_out
    return {
        "success": success,
        "exit_code": exit_code,
        "cancelled": cancelled,
        "timed_out": timed_out,
        "result_text": result_text,
        "tail": tail,
        "events": len(ev_list),
        "provider": provider,
    }


def _result_data_for(task_id: str, provider: str, model_class: str) -> dict:
    """Preserve any pre-existing result_data fields when flushing the tail so we
    don't clobber provider/model_class the launch site stored."""
    try:
        from Orchestrator.tasks import task_db
        t = task_db.get_task(task_id)
        rd = dict(t.result_data) if t and isinstance(t.result_data, dict) else {}
    except Exception:
        rd = {}
    rd.setdefault("provider", provider)
    rd.setdefault("model_class", model_class)
    return rd
