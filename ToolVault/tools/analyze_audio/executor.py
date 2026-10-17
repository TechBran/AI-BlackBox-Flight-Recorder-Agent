"""Executor for analyze_audio (migrated from blackbox_tools._execute_analyze_audio)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.utils.paths import AUDIO_EXTS, UploadPathError, resolve_upload_path


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze audio content via /analyze/audio (async task)."""
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
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                result = await resp.json()
                task_id = result.get("task_id")
                if task_id:
                    return ToolResult(
                        True,
                        f"Audio analysis queued. Task ID: {task_id}. Use get_task_status to check progress.",
                        data={"task_id": task_id}
                    )
                return ToolResult(False, f"Failed to queue audio analysis: {result}")
    except Exception as e:
        return ToolResult(False, f"Analyze audio error: {str(e)}")
