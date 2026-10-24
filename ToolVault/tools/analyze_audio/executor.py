"""Executor for analyze_audio (migrated from blackbox_tools._execute_analyze_audio)."""
import asyncio

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.utils.paths import AUDIO_EXTS, UploadPathError, resolve_upload_path

# How long the tool waits for the analysis before handing back the task id.
# Below the MCP gateway's 120 s proxy timeout, so a remote caller gets an answer.
POLL_BUDGET_SECONDS = 90
POLL_INTERVAL_SECONDS = 2.0


def _detail(body, status) -> str:
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("message")
        if detail:
            return str(detail)
    return f"HTTP {status}"


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze audio content via /analyze/audio, then poll /tasks/{id} for the result.

    The analysis runs as an AUDIO_ANALYSIS task (it uploads the file to the
    Gemini Files API, which takes long recordings). The tool waits for it and
    returns the analysis itself; only one that outlasts POLL_BUDGET_SECONDS
    comes back as a task id to check with get_task_status.
    """
    file_path = params.get("file_path", "")
    prompt = params.get("prompt", "Transcribe and describe this audio")

    if not file_path:
        return ToolResult(False, "file_path is required")
    # The file is uploaded to Gemini; refuse anything but an audio upload up
    # front (the route re-checks).
    try:
        resolve_upload_path(file_path, allowed_exts=AUDIO_EXTS)
    except UploadPathError as e:
        return ToolResult(False, f"Audio file refused (must be an audio upload): {e}")

    try:
        import aiohttp
        payload = {
            "file_path": file_path,
            "prompt": prompt,
            "operator": ctx.operator
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/analyze/audio",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json(content_type=None)
                task_id = result.get("task_id") if isinstance(result, dict) else None
                if resp.status != 200 or not task_id:
                    return ToolResult(False, f"Failed to queue audio analysis: {_detail(result, resp.status)}")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + POLL_BUDGET_SECONDS
            status = "pending"
            while True:
                async with session.get(
                    f"{ctx.base_url}/tasks/{task_id}",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    task = await resp.json(content_type=None) if resp.status == 200 else {}
                task = task if isinstance(task, dict) else {}
                status = task.get("status") or status
                if status == "completed":
                    analysis = (task.get("result_data") or {}).get("transcript")
                    if analysis:
                        return ToolResult(True, analysis, data={"task_id": task_id, "provider": "gemini"})
                    return ToolResult(False, f"Audio analysis {task_id} finished without a result",
                                      data={"task_id": task_id})
                if status in ("failed", "cancelled"):
                    reason = task.get("error_message") or "no reason given"
                    return ToolResult(False, f"Audio analysis {status}: {reason}", data={"task_id": task_id})
                if loop.time() >= deadline:
                    return ToolResult(
                        True,
                        f"Audio analysis is still running (task {task_id}, {status}). "
                        f"Use get_task_status to get the result.",
                        data={"task_id": task_id, "status": status}
                    )
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except Exception as e:
        return ToolResult(False, f"Analyze audio error: {str(e)}")
