"""Executor for get_task_status (migrated from blackbox_tools._execute_get_task_status).

NOTE: the backend route is /tasks/{task_id} (PLURAL) -- there is NO /task/{id}
(singular) handler, so an earlier /task/ path 404'd on every call. The response
fields mirror Orchestrator/routes/task_routes.py:get_task_status:
{task_id, task_type, status, progress, created_at, updated_at, result_url,
 result_data, error_message}. We surface the whole JSON so the
generate -> poll -> retrieve media loop gets status + result_url + result_data
(artifact) -- the remote MCP design polls through this tool.
"""
import json
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Check the status of an async generation task."""
    task_id = params.get("task_id", "")

    if not task_id:
        return ToolResult(False, "Task ID is required")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ctx.base_url}/tasks/{task_id}",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    status = result.get("status", "unknown")
                    # Backend field is `result_url` (not `url`); keep a `result`
                    # fallback for any legacy nested shape.
                    result_url = (
                        result.get("result_url")
                        or result.get("url")
                        or (result.get("result") or {}).get("url", "")
                    )

                    if status == "completed":
                        summary = f"Task completed! Result URL: {result_url}" if result_url \
                            else "Task completed."
                        return ToolResult(
                            success=True,
                            result=summary + "\n\n" + json.dumps(result, indent=2),
                            data=result,
                        )
                    elif status == "failed":
                        error = result.get("error_message") or result.get("error") or "Unknown error"
                        return ToolResult(
                            success=False,
                            result=f"Task failed: {error}",
                            data=result,
                        )
                    else:
                        return ToolResult(
                            success=True,
                            result=f"Task status: {status}. Still in progress...\n\n"
                                   + json.dumps(result, indent=2),
                            data=result,
                        )
                elif resp.status == 404:
                    return ToolResult(False, f"Task not found: {task_id}")
                else:
                    return ToolResult(False, f"Failed to check task status: {resp.status}")

    except Exception as e:
        return ToolResult(False, f"Task status error: {str(e)}")
