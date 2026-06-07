"""Executor for list_devices (migrated from blackbox_tools._execute_list_devices)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """List devices on the Tailscale mesh network."""
    from Orchestrator.device_registry import get_registry, DeviceType
    registry = get_registry()
    dtype = params.get("device_type")
    if dtype:
        try:
            devices = registry.get_devices_by_type(DeviceType(dtype))
        except ValueError:
            return ToolResult(False, f"Invalid device type: {dtype}. Use: android, linux, windows, macos")
    else:
        devices = registry.get_all_devices()
    if not devices:
        return ToolResult(True, "No devices registered. Add devices via POST /devices/")
    lines = []
    for d in devices:
        lines.append(f"  - {d.id}: {d.name} | {d.device_type.value} | "
                     f"{d.protocol.value} | {d.tailscale_ip} [{d.status.value}]")
        if d.description:
            lines.append(f"    {d.description}")
    return ToolResult(True, f"Devices ({len(devices)}):\n" + "\n".join(lines))
