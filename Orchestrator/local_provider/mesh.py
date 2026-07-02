"""
Tailnet mesh resolution — marry ``tailscale status`` (liveness + address) to the
local-provider attestation registry (which operator's device runs which Gemma).

The join key is the device's tailnet name (recorded at attest as ``tailnet_name``,
see ``registry.attest``). This is the device-resolution layer for ``control_phone``:
it turns "the originating operator" into "a reachable tailnet node we can POST a
task to". Degrades gracefully — if tailscale is down / unauthenticated or no device
matches, callers get ``[]`` / ``None`` rather than an exception.
"""
import json
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Optional

from .registry import get_local_registry

_TAILSCALE_TIMEOUT_SECS = 3


@dataclass
class Node:
    """A tailnet node distilled from ``tailscale status --json``."""
    hostname: str        # short name, e.g. "brandon-fold6"
    dns_name: str        # addressable MagicDNS FQDN, trailing dot stripped
    ip: str              # first IPv4 from TailscaleIPs ("" if none)
    online: bool
    os: str = ""


def parse_tailscale_status(json_str: str) -> List[Node]:
    """Parse ``tailscale status --json`` output into ``Node`` objects (Self + Peers).

    PURE: takes the raw JSON string, returns nodes. Never raises on malformed or
    empty input — returns ``[]`` so callers degrade gracefully.
    """
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    raw_nodes: List[dict] = []
    self_node = data.get("Self")
    if isinstance(self_node, dict):
        raw_nodes.append(self_node)
    peers = data.get("Peer")
    if isinstance(peers, dict):
        raw_nodes.extend(p for p in peers.values() if isinstance(p, dict))

    nodes: List[Node] = []
    for n in raw_nodes:
        ips = n.get("TailscaleIPs")
        ipv4 = ""
        if isinstance(ips, list):
            ipv4 = next((ip for ip in ips if isinstance(ip, str) and ":" not in ip), "")
        nodes.append(Node(
            hostname=(n.get("HostName") or "").strip(),
            dns_name=(n.get("DNSName") or "").rstrip("."),  # tailscale FQDN has a trailing dot
            ip=ipv4,
            online=bool(n.get("Online")),
            os=(n.get("OS") or ""),
        ))
    return nodes


def _run_tailscale_status() -> str:
    """Shell out to ``tailscale status --json`` (the mockable seam).

    Returns stdout on success, ``""`` on any failure (tailscale not installed,
    daemon down, unauthenticated). Mirrors the resilient pattern in admin_routes.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=_TAILSCALE_TIMEOUT_SECS,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _name_matches(tailnet_name: str, node: Node) -> bool:
    """True if an attested ``tailnet_name`` identifies this node.

    Matched case-insensitively against the node's short hostname or DNS name
    (full FQDN or just the first label, trailing dot ignored). This tolerates a
    device attesting either its short name ("brandon-fold6") or its MagicDNS FQDN.
    """
    if not tailnet_name:
        return False
    t = tailnet_name.strip().rstrip(".").lower()
    if not t:
        return False
    candidates = set()
    if node.hostname:
        candidates.add(node.hostname.lower())
    if node.dns_name:
        dn = node.dns_name.lower()
        candidates.add(dn)
        candidates.add(dn.split(".")[0])
    if node.ip:
        # The phone self-reports its tailnet IPv4 as the join key (it can read its own
        # 100.64/10 interface address but not its tailnet NAME), so match that too.
        candidates.add(node.ip.lower())
    # The first-label fallback (t.split(".")[0]) assumes a SINGLE tailnet — two
    # tailnets sharing a short hostname could collide here. Fine under the design's
    # single-tailnet assumption; revisit if multi-tailnet targeting ever lands.
    return t in candidates or t.split(".")[0] in candidates


def reachable_devices(operator: Optional[str] = None,
                      status_json: Optional[str] = None) -> List[dict]:
    """Online tailnet nodes that also have an attested Gemma, joined on tailnet_name.

    Each result: ``{operator, device_id, model_slug, tailnet_name, node}`` where
    ``node`` is the ``Node`` as a dict. ``operator`` filters to one operator
    (None = all). ``status_json`` overrides the shell-out (test seam); None → live
    call. Legacy attestations without a ``tailnet_name`` are skipped (unjoinable).
    """
    raw = status_json if status_json is not None else _run_tailscale_status()
    online_nodes = [n for n in parse_tailscale_status(raw) if n.online]

    pairs = get_local_registry().all_records()
    if operator:
        pairs = [(op, rec) for op, rec in pairs if op == operator]

    results: List[dict] = []
    for op, rec in pairs:
        tname = rec.get("tailnet_name")  # legacy rows lack the key entirely -> None
        if not tname:
            continue
        match = next((n for n in online_nodes if _name_matches(tname, n)), None)
        if match is None:
            continue
        results.append({
            "operator": op,
            "device_id": rec.get("device_id"),
            "model_slug": rec.get("model_slug"),
            "tailnet_name": tname,
            "node": asdict(match),
        })
    return results


def resolve_origin(operator: str, status_json: Optional[str] = None) -> Optional[Node]:
    """The originating operator's reachable device as a ``Node``, or None (v1).

    Returns the first online, attested, name-matched node for ``operator``. None
    if the operator has no attestation, no recorded tailnet_name, or the device is
    not currently online in the tailnet.
    """
    devices = reachable_devices(operator=operator, status_json=status_json)
    if not devices:
        return None
    return Node(**devices[0]["node"])


# ── M3: origin-aware routing (explicit → origin → primary → error) ──────────────────────

class DeviceResolutionError(Exception):
    """A device-control target could not be resolved. Carries a machine-readable
    ``kind`` so an executor can map it to a structured ``error_kind`` for the model.

    kind ∈ {invalid_target, origin_mismatch, no_primary_device, no_device}
      - invalid_target    : an explicit ``device`` names no reachable tailnet node
      - origin_mismatch   : the origin device is not registered to this operator —
                            the never-silent-retarget invariant. We RAISE rather
                            than fall back, so a request is never routed to a device
                            other than the one it names/originates from.
      - no_primary_device : a non-device origin, but the operator has no reachable
                            primary designated
      - no_device         : nothing resolvable at all
    """

    def __init__(self, kind: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail or {}


def _online_nodes(status_json: Optional[str]) -> List[Node]:
    """Online tailnet nodes, from an injected status_json or a live shell-out."""
    raw = status_json if status_json is not None else _run_tailscale_status()
    return [n for n in parse_tailscale_status(raw) if n.online]


def _find_node(device_key: str, nodes: List[Node]) -> Optional[Node]:
    """First node whose identity (hostname/dns/ip/short-label) matches ``device_key``."""
    return next((n for n in nodes if _name_matches(device_key, n)), None)


def _device_registry():
    """The ADB/mesh device registry singleton, or None if unavailable.

    Lazy-imported (device_registry has no dependency back on local_provider) and
    fail-soft — resolution degrades to the legacy attestation path rather than
    crashing if the registry can't load.
    """
    try:
        from Orchestrator.device_registry import get_registry
        return get_registry()
    except Exception:
        return None


def _registry_device_keys(device) -> List[str]:
    """Tailnet identity candidates for a device_registry ``Device``."""
    md = getattr(device, "metadata", None) or {}
    keys = [getattr(device, "id", ""), getattr(device, "tailscale_ip", ""),
            md.get("tailscale_dns", ""), md.get("tailscale_hostname", ""),
            getattr(device, "name", "")]
    return [k for k in keys if k]


def _match_registry_device(device, nodes: List[Node]) -> Optional[Node]:
    """The online tailnet Node this registry Device corresponds to, or None."""
    for n in nodes:
        for k in _registry_device_keys(device):
            if _name_matches(k, n):
                return n
    return None


def default_provider_for_node(node: Node, registry=None) -> Optional[str]:
    """The per-device ``default_provider`` (M3) for the registry Device that ``node`` maps to,
    or ``None`` when no device matches / none is set. This is what makes the persisted-but-
    unconsumed M3 ``default_provider`` LIVE (M7 task 7.5): control_device reads it to pick the
    frontier brain. Fail-soft (any error → ``None``); the registry is lazily resolved.
    """
    reg = registry if registry is not None else _device_registry()
    if reg is None:
        return None
    try:
        for d in reg.get_all_devices():
            if _match_registry_device(d, [node]) is not None:
                return getattr(d, "default_provider", None)
    except Exception:
        return None
    return None


def _origin_belongs_to_operator(operator: str, node: Node, registry,
                                status_json: Optional[str]) -> bool:
    """True if ``node`` (the resolved origin device) is registered to ``operator``.

    Ownership is honored from EITHER store, so a fresh box that has only one
    populated still routes correctly:
      1. the device_registry (``owner`` field), or
      2. the local-provider attestation registry (operator→attested device).
    """
    # M4 defense-in-depth: a blank operator must never "own" anything — otherwise it
    # could match an UNCLAIMED (owner=="") device_registry row and silently pass.
    if not operator:
        return False
    # 1. device_registry ownership
    if registry is not None:
        try:
            for d in registry.get_devices_by_owner(operator):
                if _match_registry_device(d, [node]) is not None:
                    return True
        except Exception:
            pass
    # 2. local-provider attestation ownership (online + name-matched to this node)
    for rec in reachable_devices(operator=operator, status_json=status_json):
        rn = Node(**rec["node"])
        if rn.dns_name == node.dns_name and rn.ip == node.ip:
            return True
    return False


def resolve_device(operator: str,
                   origin_device_id: Optional[str] = None,
                   target_device_id: Optional[str] = None,
                   status_json: Optional[str] = None,
                   registry=None) -> Node:
    """Resolve the device-control TARGET as a tailnet ``Node``, per the firm rule.

    Precedence (the locked routing invariant — see research §5.5 decision 3):
      1. explicit ``target_device_id`` → that reachable tailnet node (ANY node on the
         tailnet); if it matches no online node → raise ``invalid_target``.
      2. else ``origin_device_id`` → resolve that node, BUT if it is not registered to
         ``operator`` → raise ``origin_mismatch`` (NEVER silently retarget).
      3. else the operator's PRIMARY device (device_registry), cross-referenced with
         tailnet reachability; a designated-but-offline primary → ``no_primary_device``.
      4. else the operator's single attested reachable device (legacy resolve_origin).
      5. else → raise ``no_device``.

    ``status_json``/``registry`` are injectable test seams (None → live tailscale +
    the real registry singleton). Raises ``DeviceResolutionError``; never returns None.
    """
    target = (target_device_id or "").strip()
    origin = (origin_device_id or "").strip()

    # Materialize `tailscale status` ONCE (a live shell-out only if status_json wasn't
    # injected), then thread the captured string through every helper below — each only
    # shells out when handed None. This makes a single device-control resolve cost at
    # most ONE `tailscale status` spawn instead of the 2-3 it used to (online-nodes +
    # origin-ownership + legacy fallback each shelled independently). _run_tailscale_status
    # returns "" on failure, so `raw` is always a str and no helper re-shells.
    raw = status_json if status_json is not None else _run_tailscale_status()

    # 1. Explicit target — any reachable node on the tailnet.
    if target:
        node = _find_node(target, _online_nodes(raw))
        if node is None:
            raise DeviceResolutionError(
                "invalid_target",
                f"No reachable device named '{target}' on the tailnet. It may be "
                "offline, off the tailnet, or the name is wrong.",
                detail={"requested": target})
        return node

    # 2. Origin device — default to it, but only if it belongs to this operator.
    if origin:
        nodes = _online_nodes(raw)
        node = _find_node(origin, nodes)
        if node is None:
            raise DeviceResolutionError(
                "no_device",
                f"The originating device '{origin}' is not reachable on the tailnet "
                "right now.",
                detail={"origin": origin})
        reg = registry if registry is not None else _device_registry()
        if not _origin_belongs_to_operator(operator, node, reg, raw):
            raise DeviceResolutionError(
                "origin_mismatch",
                f"The originating device '{origin}' is not registered to operator "
                f"'{operator}'. Refusing to silently retarget — name an explicit "
                "device to run elsewhere.",
                detail={"origin": origin, "operator": operator})
        return node

    # 3. Operator's PRIMARY device, cross-referenced with reachability.
    reg = registry if registry is not None else _device_registry()
    primary = None
    if reg is not None:
        try:
            primary = reg.get_primary_device(operator)
        except Exception:
            primary = None
    if primary is not None:
        node = _match_registry_device(primary, _online_nodes(raw))
        if node is not None:
            return node
        raise DeviceResolutionError(
            "no_primary_device",
            f"Operator '{operator}' has a primary device ('{primary.id}') but it is "
            "not reachable on the tailnet right now.",
            detail={"operator": operator, "primary": primary.id})

    # 4. Legacy fallback: the operator's single attested reachable device.
    legacy = resolve_origin(operator, status_json=raw)
    if legacy is not None:
        return legacy

    # 5. Nothing resolvable.
    raise DeviceResolutionError(
        "no_device",
        f"No reachable device for operator '{operator}' — none is online, and none "
        "is designated as the primary device.",
        detail={"operator": operator})
