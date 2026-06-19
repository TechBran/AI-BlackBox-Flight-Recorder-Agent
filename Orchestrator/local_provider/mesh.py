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
