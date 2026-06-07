"""Executor for get_task_status (migrated from blackbox_tools._execute_get_task_status)."""
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
                f"{ctx.base_url}/task/{task_id}",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    status = result.get("status", "unknown")

                    if status == "completed":
                        url = result.get("url", result.get("result", {}).get("url", ""))
                        return ToolResult(
                            success=True,
                            result=f"Task completed! Result URL: {url}",
                            data={"status": status, "url": url, "result": result}
                        )
                    elif status == "failed":
                        error = result.get("error", "Unknown error")
                        return ToolResult(
                            success=False,
                            result=f"Task failed: {error}",
                            data={"status": status, "error": error}
                        )
                    else:
                        return ToolResult(
                            success=True,
                            result=f"Task status: {status}. Still in progress...",
                            data={"status": status}
                        )
                elif resp.status == 404:
                    return ToolResult(False, f"Task not found: {task_id}")
                else:
                    return ToolResult(False, f"Failed to check task status: {resp.status}")

    except Exception as e:
        return ToolResult(False, f"Task status error: {str(e)}")
