"""G2-T10 (M2.2) — shared support for the three CLI-agent ToolVault tools.

`claude_code_task`, `gemini_cli_task`, and `codex_cli_task` are deliberately thin
(each executor is one line). The security-critical logic — an HONEST per-provider
authentication check plus the fully-open (YOLO) task launch — lives here, in ONE
place, so it can never drift between the three tools.

Shape mirrors ToolVault/tools/use_computer/executor.py exactly: every failure is a
structured payload ``{"success": False, "retryable": <bool>, "reason": "...", ...}``
serialized into ``ToolResult.result`` (the chat path forwards ``.result`` and DROPS
``.data``) AND mirrored into ``.data`` (the voice surfaces read ``rich_result()``/
``.data``). Both callers must see the failure.

This module owns NO argv/spawn/model-resolution logic — that is `headless.py`. It
imports only `headless.CLAUDE_MODEL_CLASSES` so claude's class set has a single
source of truth.

Auth checks (verified on this box 2026-07-10; see headless.py's WARNING):
  * claude  -> ~/.claude/.credentials.json exists.
  * gemini  -> ~/.gemini/oauth_creds.json exists (SPECIFICALLY — .gemini/settings.json
               is created by merely running the CLI once, so it false-positives).
  * codex   -> ~/.codex/auth.json carries a non-empty ``auth_mode``. Presence != mode;
               the file ALSO holds a token + a stored OPENAI_API_KEY, so we read ONLY
               ``auth_mode`` and NEVER surface a secret value.

The checks are pure local filesystem work — a stat plus a sub-2KB JSON parse, no
network — so they run inline on the event loop (unlike use_computer's catalog
fetch, which can hit the network on a cold cache and is therefore threaded). A
credential stat is microseconds; a to_thread hop would cost more than it saves.
"""
import json
import os
from typing import Optional

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.cli_agent.headless import CLAUDE_MODEL_CLASSES


# provider -> the command the operator runs to sign that CLI in (named in the
# fail-fast reason so the calling model/voice agent can tell the operator).
_SIGN_IN = {
    "claude": "claude (then complete the sign-in)",
    "gemini": "gemini (then choose 'Login with Google' and sign in)",
    "codex": "codex login",
}


def _home() -> str:
    # expanduser honors $HOME on POSIX, so tests can point at a fake home.
    return os.path.expanduser("~")


def _claude_authenticated() -> bool:
    return os.path.exists(os.path.join(_home(), ".claude", ".credentials.json"))


def _gemini_authenticated() -> bool:
    # ONLY oauth_creds.json — NOT .gemini/settings.json (the wizard false-positive).
    return os.path.exists(os.path.join(_home(), ".gemini", "oauth_creds.json"))


def _codex_authenticated() -> bool:
    """codex is signed in iff ~/.codex/auth.json carries a non-empty auth_mode.
    We read ONLY auth_mode; the token + stored OPENAI_API_KEY in that file are
    never touched or surfaced."""
    path = os.path.join(_home(), ".codex", "auth.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    mode = data.get("auth_mode")
    return isinstance(mode, str) and bool(mode.strip())


_AUTH_CHECKS = {
    "claude": _claude_authenticated,
    "gemini": _gemini_authenticated,
    "codex": _codex_authenticated,
}


def _auth_failure(provider: str) -> Optional[dict]:
    """None if the provider CLI is authenticated; else a structured, NON-retryable
    failure payload (retrying without signing in cannot help — retryable=False)."""
    check = _AUTH_CHECKS.get(provider)
    if check is None:
        return {"success": False, "retryable": False,
                "reason": f"Unsupported CLI-agent provider {provider!r}.",
                "provider": provider}
    if check():
        return None
    return {
        "success": False,
        "retryable": False,
        "reason": (f"The {provider} CLI is not signed in on this machine. "
                   f"Run `{_SIGN_IN[provider]}` in a terminal, then try again."),
        "provider": provider,
        "authenticated": False,
    }


def _model_failure(provider: str, model: Optional[str]) -> Optional[dict]:
    """claude is the ONLY genuine class resolver: validate its class up front so
    the caller gets a structured, RETRYABLE error naming the valid classes rather
    than a FAILED task from build_argv. gemini/codex take a concrete id verbatim,
    so there is nothing to validate (empty -> the CLI's own default)."""
    if provider == "claude" and model and model not in CLAUDE_MODEL_CLASSES:
        return {
            "success": False,
            "retryable": True,  # retrying WITH a valid class resolves it
            "reason": (f"Unknown claude model class {model!r}. Valid classes: "
                       f"{', '.join(CLAUDE_MODEL_CLASSES)}. Claude resolves a class "
                       f"to its newest version."),
            "allowed": list(CLAUDE_MODEL_CLASSES),
        }
    return None


async def launch(provider: str, params: dict, ctx: ToolContext) -> ToolResult:
    """Auth fail-fast -> model-class check -> create a fully-open CLI_AGENT task.

    Returns the task_id immediately; the caller polls get_task_status (T9 runner +
    worker + T8 cancel do the rest). permission_mode is always "yolo" (Brandon's
    explicit decision for this tailnet surface — the reason T8's kill switch and
    the D1 no-`mcp` rule had to land first)."""
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return ToolResult(False, "prompt is required")

    # Normalize empties to None: omitted/blank model -> the CLI's own default;
    # omitted/blank cwd -> the worker defaults it (task_rd.get("cwd") or getcwd()).
    model = (params.get("model") or "").strip() or None
    cwd = (params.get("cwd") or "").strip() or None

    # 1) Honest auth check — BEFORE any task is created.
    fail = _auth_failure(provider)
    if fail is not None:
        return ToolResult(False, json.dumps(fail), data=fail)

    # 2) claude-only model-class validation (structured + retryable).
    fail = _model_failure(provider, model)
    if fail is not None:
        return ToolResult(False, json.dumps(fail), data=fail)

    # 3) Launch the async, fully-open task.
    try:
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType
        result_data = {
            "provider": provider,
            "model": model,
            "cwd": cwd,
            "permission_mode": "yolo",
        }
        task = create_task(
            TaskType.CLI_AGENT,
            operator=ctx.operator,
            prompt=prompt,
            result_data=result_data,
        )
        return ToolResult(
            True,
            (f"{provider} CLI agent task started. Task ID: {task.task_id}. "
             f"Poll get_task_status for progress and the result; cancel it via the "
             f"task cancel endpoint."),
            data={"task_id": task.task_id, "provider": provider},
        )
    except Exception as e:  # never let an exception escape the executor
        return ToolResult(False, f"Failed to start {provider} CLI agent task: {e}")
