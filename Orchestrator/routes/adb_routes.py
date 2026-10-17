"""REST API routes for ADB device control."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from Orchestrator.adb import get_adb_manager, ADBCommands
from Orchestrator.device_registry import get_registry

router = APIRouter(prefix="/adb", tags=["adb"])


class TapRequest(BaseModel):
    device_id: str
    x: int
    y: int
    normalized: bool = True

class SwipeRequest(BaseModel):
    device_id: str
    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int = 300
    normalized: bool = True

class TypeRequest(BaseModel):
    device_id: str
    text: str

class KeyRequest(BaseModel):
    device_id: str
    keycode: str

class PairRequest(BaseModel):
    device_id: str
    pairing_port: int
    pairing_code: str

class SmartPairRequest(BaseModel):
    device_id: str
    pairing_port: int
    pairing_code: str

class PairFromDeviceRequest(BaseModel):
    tailscale_ip: str
    pairing_port: int
    pairing_code: str

class SetPortRequest(BaseModel):
    device_id: str
    connection_port: int

class AppRequest(BaseModel):
    device_id: str
    package_name: str
    activity: Optional[str] = None


@router.post("/pair")
async def pair_device(body: PairRequest):
    mgr = get_adb_manager()
    result = await mgr.pair(body.device_id, body.pairing_port, body.pairing_code)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


def _refuse_invalid(result: dict) -> dict:
    """ADBCommands refuses a hostile/malformed keycode, package or activity
    without touching the device (audit O14-cli-agents-devices-01); surface
    that as a 400 instead of a 200 that looks like a device-side failure."""
    if result.get("invalid_input"):
        raise HTTPException(status_code=400, detail=result.get("error", "invalid input"))
    return result


def _validate_port(port: int, field_name: str = "port"):
    """Validate port is in range 1-65535."""
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: {port} (must be 1-65535)")


@router.post("/smart-pair")
async def smart_pair(body: SmartPairRequest):
    """Full smart pairing workflow with step-by-step progress."""
    _validate_port(body.pairing_port, "pairing_port")
    mgr = get_adb_manager()
    return await mgr.smart_pair(body.device_id, body.pairing_port, body.pairing_code)


@router.post("/pair-from-device")
async def pair_from_device(body: PairFromDeviceRequest):
    """Android app relay: pair by Tailscale IP (creates device if needed)."""
    _validate_port(body.pairing_port, "pairing_port")
    mgr = get_adb_manager()
    return await mgr.pair_from_ip(body.tailscale_ip, body.pairing_port, body.pairing_code)


@router.post("/smart-pair/set-port")
async def smart_pair_set_port(body: SetPortRequest):
    """Fallback: manually set connection port after pairing succeeded."""
    _validate_port(body.connection_port, "connection_port")
    mgr = get_adb_manager()
    result = await mgr.set_connection_port(body.device_id, body.connection_port)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/connect/{device_id}")
async def connect_device(device_id: str):
    mgr = get_adb_manager()

    # First try direct connect with stored port
    result = await mgr.connect(device_id)
    if result["success"]:
        return result

    # If direct connect failed, try port rediscovery
    from Orchestrator.device_registry import get_registry
    device = get_registry().get_device(device_id)
    if device:
        discovered_port = await mgr.discover_connection_port(device.tailscale_ip, device.adb_port)
        if discovered_port and discovered_port != device.adb_port:
            # Update port and retry
            get_registry().update_device(device_id, adb_port=discovered_port)
            result = await mgr.connect(device_id)
            if result["success"]:
                return result

    # Still failed — provide helpful error
    error = result.get("error", "Connection failed")
    if "closed" in error.lower() or "refused" in error.lower() or "failed to auth" in error.lower():
        error += ". Wireless debugging may have been toggled — try re-pairing the device."
    raise HTTPException(status_code=400, detail=error)


@router.post("/disconnect/{device_id}")
async def disconnect_device(device_id: str):
    mgr = get_adb_manager()
    return await mgr.disconnect(device_id)


@router.get("/devices")
async def list_connected():
    mgr = get_adb_manager()
    try:
        return {"devices": await mgr.list_connected()}
    except Exception as e:
        return {"devices": [], "error": str(e)}


@router.get("/screenshot/{device_id}")
async def get_screenshot(device_id: str):
    mgr = get_adb_manager()
    result = await mgr.ensure_connected(device_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Connection failed"))
    cmds = ADBCommands(device_id)
    await cmds.detect_screen_size()
    b64 = await cmds.screenshot_base64()
    return {"device_id": device_id, "screenshot_base64": b64,
            "screen_size": [cmds.screen_width, cmds.screen_height]}


@router.post("/tap")
async def tap(body: TapRequest):
    cmds = ADBCommands(body.device_id)
    await cmds.detect_screen_size()
    return await cmds.tap(body.x, body.y, body.normalized)


@router.post("/swipe")
async def swipe(body: SwipeRequest):
    cmds = ADBCommands(body.device_id)
    await cmds.detect_screen_size()
    return await cmds.swipe(body.x1, body.y1, body.x2, body.y2,
                            body.duration_ms, body.normalized)


@router.post("/type")
async def type_text(body: TypeRequest):
    cmds = ADBCommands(body.device_id)
    return await cmds.type_text(body.text)


@router.post("/key")
async def key_event(body: KeyRequest):
    cmds = ADBCommands(body.device_id)
    return _refuse_invalid(await cmds.key_event(body.keycode))


@router.post("/open-app")
async def open_app(body: AppRequest):
    cmds = ADBCommands(body.device_id)
    return _refuse_invalid(await cmds.open_app(body.package_name, body.activity))


@router.post("/home/{device_id}")
async def go_home(device_id: str):
    cmds = ADBCommands(device_id)
    return await cmds.go_home()


@router.post("/back/{device_id}")
async def go_back(device_id: str):
    cmds = ADBCommands(device_id)
    return await cmds.go_back()
