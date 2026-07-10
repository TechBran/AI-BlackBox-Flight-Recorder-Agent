"""Executor for use_computer (migrated from blackbox_tools._execute_use_computer).

The `model` param carries a stable CU model CLASS (or a concrete id); the
concrete id is resolved here — at task-creation time — against the live CU
catalog via Orchestrator.browser.dispatch.resolve_model_class. Resolving here
(not in the headless runner) means the runner's existing resolve_backend(model)
call keeps working unchanged: it just receives an already-concrete id.
"""
import asyncio
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult

# The closed CU class set (mirrors the schema text). `haiku` has NO CU support
# and is intentionally absent. Used only to report which classes the LIVE
# catalog can currently satisfy when resolution fails.
_CU_MODEL_CLASSES = ("opus", "sonnet", "fable", "gemini", "gpt")


def _available_cu_classes() -> list:
    """Which closed CU classes the LIVE catalog can currently serve.

    Sync (does a cached catalog fetch); call OFF the event loop. Uses dispatch's
    PUBLIC resolver so there is no duplicated class/backend logic here.
    """
    from Orchestrator.browser.dispatch import resolve_model_class
    from Orchestrator.routes.admin_routes import get_available_models

    catalog = get_available_models("computer-use").get("models", [])
    available = []
    for cls in _CU_MODEL_CLASSES:
        try:
            resolve_model_class(cls, catalog=catalog)
            available.append(cls)
        except ValueError:
            pass
    return available


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Launch a Computer Use agent on a Linux desktop (backend derived from the
    resolved model class)."""
    prompt = params.get("prompt", "")
    url = params.get("url")
    device_id = params.get("device_id", "blackbox")
    model = params.get("model")  # class name or concrete id; None -> default class

    if not prompt:
        return ToolResult(False, "Prompt is required for use_computer")

    from Orchestrator.browser.dispatch import resolve_model_class

    # resolve_model_class is SYNC and may perform a cached network fetch on a
    # cold cache (catalog=None path) — keep it off the event loop.
    try:
        resolved_model = await asyncio.to_thread(resolve_model_class, model)
    except ValueError as e:
        # Structured, retryable failure: the calling LLM asked for a class we
        # cannot serve and must be able to retry with one we can. The main chat
        # path forwards ToolResult.result (the string) to the model, so the
        # machine-actionable payload lives THERE; data mirrors it for the voice
        # surfaces (which read rich_result()/data).
        available = await asyncio.to_thread(_available_cu_classes)
        payload = {
            "success": False,
            "retryable": True,
            "reason": str(e),
            "available": available,
        }
        return ToolResult(False, json.dumps(payload), data=payload)

    try:
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType
        result_data = {"device_id": device_id, "model": resolved_model}
        if url:
            result_data["url"] = url
        task = create_task(
            TaskType.USE_COMPUTER,
            operator=ctx.operator,
            prompt=prompt,
            result_data=result_data
        )
        return ToolResult(
            True,
            f"Computer Use task started. Task ID: {task.task_id}. Use get_task_status to check progress.",
            data={"task_id": task.task_id}
        )
    except Exception as e:
        return ToolResult(False, f"Computer use error: {str(e)}")
