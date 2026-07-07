"""REST API routes for the Device Registry.

Two layers live here:
  * the original per-device CRUD (``GET /devices/``, ``POST /devices/`` …), driven
    purely by the registry, and
  * the M3 origin-aware DEVICE-MANAGEMENT surface (``GET /devices/mesh`` + the
    ``/operator`` / ``/primary`` / ``/default-provider`` mutators) that the Portal +
    Android "Devices" System-Menu views (a later UI wave) consume. The mesh view
    JOINS ``tailscale status`` (all tailnet nodes + liveness) with the registry
    (ownership, primary flag, default provider) so the UI shows the WHOLE tailnet,
    not just already-registered devices.

Trust model + ownership provenance
----------------------------------
The device-management surface enforces a light authorization model suited to the
box's trust boundary — the TAILSCALE PERIMETER with cooperating (non-adversarial)
operators. A device's ``owner`` is the ownership source of truth for origin-aware
routing (see ``mesh.resolve_device``), so it is set ONLY via the authenticated
``POST /{id}/operator`` (assign) path: the legacy create/update routes NEVER accept
an ``owner`` from the request body, and assign REFUSES (409) to re-home a device that
is already owned by a different operator (a deliberate re-home must first unassign it).
This keeps casual cross-operator device mutation out while staying simple for the
trusted mesh.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from Orchestrator.device_registry import (
    get_registry, Device, DeviceType, DeviceProtocol
)
from Orchestrator.local_provider import mesh

router = APIRouter(prefix="/devices", tags=["devices"])


# --------------------------------------------------------------------------- #
# M3 device-management helpers (tailnet join + operator authorization)
# --------------------------------------------------------------------------- #
def _get_tailnet_nodes() -> List[mesh.Node]:
    """All tailnet nodes (online + offline), from ``tailscale status``. Mockable
    seam for tests; degrades to ``[]`` if tailscale is down/unauthenticated."""
    return mesh.parse_tailscale_status(mesh._run_tailscale_status())


def _live_operators() -> List[str]:
    """This box's live operator roster — the SAME source GET /operators serves."""
    from Orchestrator.config import USERS_LIST
    return list(USERS_LIST)


def _require_live_operator(operator: str) -> str:
    """Validate + normalize an operator for a mutating call (consistent with the
    onboarding/MCP routes). Raises HTTP 400 on blank/'system'/unknown operators."""
    op = (operator or "").strip()
    if not op:
        raise HTTPException(status_code=400, detail="operator is required")
    if op.lower() == "system":
        raise HTTPException(status_code=400,
                            detail="'system' is not assignable as a device owner")
    if op not in _live_operators():
        raise HTTPException(status_code=400,
                            detail=f"operator {op!r} is not a live operator on this box")
    return op


def _os_to_type(os_name: Optional[str]) -> str:
    o = (os_name or "").lower()
    if o == "android":
        return "android"
    if o == "windows":
        return "windows"
    if o in ("macos", "ios"):
        return "macos"
    return "linux"


def _node_slug(node: mesh.Node) -> str:
    """Stable id for an un-registered tailnet node (mirrors the sync_from_tailscale
    convention: DNS first-label, else hostname, else IP)."""
    if node.dns_name:
        return node.dns_name.split(".")[0].lower()
    if node.hostname and node.hostname.lower() != "localhost":
        return node.hostname.lower().replace(" ", "-")
    return f"device-{(node.ip or 'unknown').replace('.', '-')}"


def _match_node_to_device(node: mesh.Node, registry) -> Optional[Device]:
    """The registry Device this tailnet node corresponds to, or None."""
    for d in registry.get_all_devices():
        if mesh._match_registry_device(d, [node]) is not None:
            return d
    return None


def _mesh_entry(node: Optional[mesh.Node], device: Optional[Device]) -> dict:
    """One row of the mesh join: identity + liveness + ownership annotations."""
    if device is not None:
        dns = (device.metadata or {}).get("tailscale_dns", "").rstrip(".")
        return {
            "id": device.id,
            "name": device.name,
            "tailnet": (node.dns_name or node.ip) if node else (dns or device.tailscale_ip),
            "type": device.device_type.value,
            "online": bool(node.online) if node else False,
            "owner": device.owner or None,
            "is_primary": bool(device.is_primary),
            "default_provider": device.default_provider,
        }
    # Un-registered tailnet node (no ownership yet).
    return {
        "id": _node_slug(node),
        "name": node.hostname or _node_slug(node),
        "tailnet": node.dns_name or node.ip,
        "type": _os_to_type(node.os),
        "online": bool(node.online),
        "owner": None,
        "is_primary": False,
        "default_provider": None,
    }


def _autoregister_from_tailnet(device_id: str, registry) -> Optional[Device]:
    """Create a registry Device for a live tailnet node so ownership can be assigned
    to a device that was never sync'd. Returns the created Device, or None if no
    tailnet node matches ``device_id``."""
    for n in _get_tailnet_nodes():
        if mesh._name_matches(device_id, n):
            dtype = _os_to_type(n.os)
            device = Device(
                id=_node_slug(n),
                name=n.hostname or _node_slug(n),
                tailscale_ip=n.ip or "",
                device_type=DeviceType(dtype),
                protocol=DeviceProtocol.ADB if dtype == "android" else DeviceProtocol.VNC,
                owner="",  # caller assigns
                description=f"Registered from tailnet ({n.os or 'unknown'})",
                metadata={
                    "tailscale_dns": (n.dns_name + ".") if n.dns_name else "",
                    "tailscale_hostname": n.hostname,
                    "tailscale_online": n.online,
                },
            )
            registry.add_device(device)
            return device
    return None


class DeviceCreate(BaseModel):
    id: str
    name: str
    tailscale_ip: str
    device_type: str
    protocol: str
    # owner is accepted for back-compat but IGNORED (I2): a created device is always
    # UNCLAIMED — ownership is set only via the authenticated POST /{id}/operator path.
    owner: Optional[str] = ""
    description: str = ""
    adb_port: int = 5555
    vnc_port: int = 5900
    rdp_port: int = 3389
    metadata: Dict[str, Any] = {}


class DeviceUpdate(BaseModel):
    # NB: owner / is_primary / default_provider are deliberately NOT updatable here
    # (I2) — they are mutated only via their authenticated routes (/operator, /primary,
    # /default-provider) so ownership + routing state can't be forged through PUT.
    name: Optional[str] = None
    tailscale_ip: Optional[str] = None
    description: Optional[str] = None
    adb_port: Optional[int] = None
    vnc_port: Optional[int] = None
    rdp_port: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


@router.get("/")
async def list_devices(owner: Optional[str] = None, device_type: Optional[str] = None):
    registry = get_registry()
    if owner:
        devices = registry.get_devices_by_owner(owner)
    elif device_type:
        devices = registry.get_devices_by_type(DeviceType(device_type))
    else:
        devices = registry.get_all_devices()
    return {"devices": [d.to_dict() for d in devices]}


@router.post("/sync-tailscale")
async def sync_tailscale():
    """Auto-discover devices from the Tailscale network and add/update the registry."""
    registry = get_registry()
    try:
        results = await registry.sync_from_tailscale()
        return {
            "status": "synced",
            "results": results,
            "total_devices": len(registry.get_all_devices())
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------------------- #
# M3 device-management API (consumed by the Portal + Android "Devices" views)
# NB: /mesh + these string-suffixed routes are declared BEFORE GET /{device_id}
# so "mesh" is never captured as a device id.
# --------------------------------------------------------------------------- #
class OperatorBody(BaseModel):
    operator: str


class DefaultProviderBody(BaseModel):
    provider: Optional[str] = None   # gemma|gemini|claude|openai | null (clears)
    operator: Optional[str] = None   # REQUIRED at the route (ownership isolation)


@router.get("/mesh")
async def list_mesh_devices(operator: Optional[str] = None):
    """ALL tailnet nodes joined with registry ownership — the source of truth for the
    System-Menu Devices views. Each row: {id, name, tailnet, type, online, owner,
    is_primary, default_provider}. ``operator`` (optional) filters to that operator's
    devices plus still-unclaimed nodes (so they can be assigned)."""
    registry = get_registry()
    nodes = _get_tailnet_nodes()
    rows: List[dict] = []
    matched_ids = set()
    for n in nodes:
        d = _match_node_to_device(n, registry)
        if d is not None:
            matched_ids.add(d.id)
        rows.append(_mesh_entry(n, d))
    # Registry devices with no live tailnet node (offline / not yet discovered).
    for d in registry.get_all_devices():
        if d.id not in matched_ids:
            rows.append(_mesh_entry(None, d))
    if operator:
        op = operator.lower()
        rows = [r for r in rows if r["owner"] is None or r["owner"].lower() == op]
    return {"devices": rows, "operator": operator}


@router.post("/{device_id}/operator")
async def assign_operator(device_id: str, body: OperatorBody):
    """Assign an operator↔device (claim a device for an operator). Auto-registers a
    live tailnet node that was never sync'd. Claiming clears the primary flag so a
    primary never leaks across owners.

    REFUSES (409) to re-home a device that is ALREADY owned by a DIFFERENT operator —
    a deliberate re-home must first unassign it — so ownership can't be silently stolen.
    Claiming an UNCLAIMED (owner-blank) device, or re-affirming the SAME owner, still
    succeeds."""
    operator = _require_live_operator(body.operator)
    registry = get_registry()
    device = registry.get_device(device_id)
    if device is None:
        device = _autoregister_from_tailnet(device_id, registry)
        if device is None:
            raise HTTPException(status_code=404,
                                detail=f"Device not found in registry or on the tailnet: {device_id}")
    existing_owner = (device.owner or "").strip()
    if existing_owner and existing_owner.lower() != operator.lower():
        raise HTTPException(
            status_code=409,
            detail=f"device already owned by {existing_owner}; unassign it first")
    device = registry.update_device(device.id, owner=operator, is_primary=False)
    return {"status": "assigned", "device": device.to_dict()}


@router.post("/{device_id}/primary")
async def set_primary(device_id: str, body: OperatorBody):
    """Designate the operator's primary device (clears the old primary atomically).
    Operator-isolated: fails if the device is not owned by this operator."""
    operator = _require_live_operator(body.operator)
    registry = get_registry()
    device = registry.set_primary_device(operator, device_id)
    if device is None:
        if registry.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
        raise HTTPException(status_code=403,
                            detail=f"Device {device_id} is not owned by operator {operator!r}")
    return {"status": "primary_set", "device": device.to_dict()}


@router.post("/{device_id}/default-provider")
async def set_default_provider(device_id: str, body: DefaultProviderBody):
    """Set (or clear) a device's default frontier provider. Operator-isolated like
    ``/primary``: an ``operator`` is ALWAYS required (400 if blank) and must own the
    device (403 otherwise) — consistent enforcement, not conditional on the body."""
    operator = _require_live_operator(body.operator)
    registry = get_registry()
    device = registry.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    if (device.owner or "").lower() != operator.lower():
        raise HTTPException(status_code=403,
                            detail=f"Device {device_id} is not owned by operator {operator!r}")
    try:
        device = registry.set_default_provider(device_id, body.provider)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "provider_set", "device": device.to_dict()}


@router.post("/{device_id}/unassign")
async def unassign_operator(device_id: str, body: OperatorBody):
    """Clear a device's owner (and primary flag) so it can be re-homed. Requires a
    live operator in the body for provenance/logging; permitted within the
    cooperative tailnet trust model (the UI confirms cross-operator re-homes)."""
    _require_live_operator(body.operator)  # provenance; 400 on blank/system/unknown
    registry = get_registry()
    device = registry.clear_owner(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    return {"status": "unassigned", "device": device.to_dict()}


@router.get("/{device_id}")
async def get_device(device_id: str):
    registry = get_registry()
    device = registry.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    return device.to_dict()


@router.post("/")
async def add_device(body: DeviceCreate):
    """Create a registry device. The device is always created UNCLAIMED — any ``owner``
    in the body is IGNORED (I2, unforgeable ownership); claim it afterwards via the
    authenticated POST /{id}/operator path."""
    registry = get_registry()
    if registry.get_device(body.id):
        raise HTTPException(status_code=409, detail=f"Device already exists: {body.id}")
    device = Device(
        id=body.id, name=body.name, tailscale_ip=body.tailscale_ip,
        device_type=DeviceType(body.device_type),
        protocol=DeviceProtocol(body.protocol),
        owner="",  # I2: never trust body.owner — ownership is set only via /operator.
        description=body.description,
        adb_port=body.adb_port, vnc_port=body.vnc_port,
        rdp_port=body.rdp_port, metadata=body.metadata,
    )
    registry.add_device(device)
    return {"status": "created", "device": device.to_dict()}


@router.put("/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate):
    registry = get_registry()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    # Validate port ranges before persisting
    for port_field in ("adb_port", "vnc_port", "rdp_port"):
        if port_field in updates:
            port_val = updates[port_field]
            if not isinstance(port_val, int) or port_val < 1 or port_val > 65535:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid {port_field}: {port_val} (must be 1-65535)"
                )
    device = registry.update_device(device_id, **updates)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    return {"status": "updated", "device": device.to_dict()}


@router.delete("/{device_id}")
async def remove_device(device_id: str):
    registry = get_registry()
    if not registry.remove_device(device_id):
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    return {"status": "removed", "device_id": device_id}


@router.get("/{device_id}/health")
async def check_device_health(device_id: str):
    registry = get_registry()
    device = registry.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
    status = await registry.check_device_health(device_id)
    return {"device_id": device_id, "status": status.value, "last_seen": device.last_seen}


@router.post("/health/all")
async def check_all_health():
    registry = get_registry()
    results = await registry.check_all_health()
    return {"results": {k: v.value for k, v in results.items()}}


@router.get("/context/prompt")
async def get_prompt_context(owner: Optional[str] = None):
    registry = get_registry()
    return {"context": registry.to_prompt_context(owner)}
