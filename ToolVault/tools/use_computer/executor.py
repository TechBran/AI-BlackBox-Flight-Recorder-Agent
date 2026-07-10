"""Executor for use_computer (migrated from blackbox_tools._execute_use_computer).

The `model` param carries a stable CU model CLASS (or a concrete id); the
concrete id is resolved here — at task-creation time — against the live CU
catalog via Orchestrator.browser.dispatch. Resolving here (not in the headless
runner) means the runner's existing resolve_backend(model) call keeps working
unchanged: it just receives an already-concrete id.
"""
import asyncio
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


def _fetch_cu_catalog() -> list:
    """Live CU catalog model list. SYNC — does a cached vendor fetch (may hit
    the network on a cold cache), so call it OFF the event loop. Never raises:
    a total outage yields a static fallback list."""
    from Orchestrator.routes.admin_routes import get_available_models
    return get_available_models("computer-use").get("models", [])


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Launch a Computer Use agent on a Linux desktop (backend derived from the
    resolved model class)."""
    prompt = params.get("prompt", "")
    url = params.get("url")
    device_id = params.get("device_id", "blackbox")
    model = params.get("model")  # class name or concrete id; None -> default class

    if not prompt:
        return ToolResult(False, "Prompt is required for use_computer")

    from Orchestrator.browser import dispatch

    # ONE catalog fetch, kept off the event loop (see _fetch_cu_catalog). It
    # backs both the resolution below AND the available-classes report on the
    # failure path — pass it in explicitly so nothing re-fetches or re-probes.
    catalog = await asyncio.to_thread(_fetch_cu_catalog)

    try:
        resolved_model = dispatch.resolve_model_class(model, catalog=catalog)
    except ValueError as e:
        # Structured, retryable failure so the calling LLM can retry with a class
        # we CAN serve. The main chat path forwards ToolResult.result (the
        # string) to the model, so the machine-actionable payload lives THERE;
        # data mirrors it for the voice surfaces (which read rich_result()/data).
        # `available` comes from dispatch (single source of truth), and
        # `retryable` tracks it: an empty list means there is nothing to retry
        # with (total outage) — telling the model to retry then would spin.
        available = dispatch.available_classes(catalog)
        payload = {
            "success": False,
            "retryable": bool(available),
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
