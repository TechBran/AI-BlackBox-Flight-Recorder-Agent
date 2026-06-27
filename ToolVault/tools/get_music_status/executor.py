"""Executor for get_music_status.

Pattern mirrors get_task_status/executor.py. GETs the live /music/status backend
endpoint (Orchestrator/routes/tts_routes.py:/music/status), which returns
{lyria_available, model, ...}, and surfaces that JSON. Despite the schema's
"alias for get_task_status" wording, the live MCP branch this replaces hit
/music/status (NOT a task lookup), so the task_id param is not required here.
"""
import json
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Check Lyria music-generation availability/status."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ctx.base_url}/music/status",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return ToolResult(
                        success=True,
                        result=json.dumps(result, indent=2),
                        data=result,
                    )
                return ToolResult(False, f"Failed to check music status: {resp.status}")
    except Exception as e:
        return ToolResult(False, f"Music status error: {str(e)}")
