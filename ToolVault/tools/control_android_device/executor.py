"""Executor for control_android_device (migrated from blackbox_tools._execute_control_android_device)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Control an Android device via Gemini Computer Use."""
    prompt = params.get("prompt", "")
    device_id = params.get("device_id", "")

    if not prompt:
        return ToolResult(False, "Prompt is required")
    if not device_id:
        return ToolResult(False, "device_id is required. Use list_devices to see available devices.")

    try:
        import asyncio
        from Orchestrator.device_registry import get_registry, DeviceProtocol
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType
        from Orchestrator.gemini_cu import get_or_create_session, run_gemini_cu_loop
        from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL
        from Orchestrator.routes.gemini_cu_routes import _run_task

        # Validate device exists and is ADB
        registry = get_registry()
        device = registry.get_device(device_id)
        if not device:
            return ToolResult(False, f"Device not found: {device_id}")
        if device.protocol != DeviceProtocol.ADB:
            return ToolResult(False, f"Device {device_id} is not an ADB device")

        # Ensure ADB connection
        from Orchestrator.adb import get_adb_manager
        conn_result = await get_adb_manager().ensure_connected(device_id)
        if not conn_result["success"]:
            return ToolResult(False, f"Cannot connect to device: {conn_result.get('error')}")

        # Create task
        task = create_task(
            TaskType.GEMINI_CU,
            operator=ctx.operator,
            prompt=prompt,
            result_data={
                "device_id": device_id,
                "environment": "android",
                "model": DEFAULT_CU_MODEL,
                "url": None,
            }
        )

        # Fire-and-forget the background task
        asyncio.create_task(_run_task(
            task.task_id, ctx.operator, device_id, "android",
            prompt, DEFAULT_CU_MODEL, None, None
        ))

        return ToolResult(
            True,
            f"Android CU task started on device '{device_id}'. Task ID: {task.task_id}. "
            f"Use get_task_status to check progress.",
            data={"task_id": task.task_id}
        )
    except Exception as e:
        return ToolResult(False, f"Error: {str(e)}")
