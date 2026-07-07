"""
Device Registry — manages all devices on the Tailscale mesh.

Usage:
    from Orchestrator.device_registry import get_registry
    registry = get_registry()
    device = registry.get_device("brandon-phone")
    android_devices = registry.get_devices_by_type(DeviceType.ANDROID)
"""
import json
import asyncio
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Dict
from .models import (
    Device, DeviceType, DeviceProtocol, DeviceStatus,
    VALID_DEFAULT_PROVIDERS, sanitize_default_provider,
)

DEVICES_FILE = Path(__file__).parent / "devices.json"


class DeviceRegistry:
    """Singleton registry of all controllable devices on the Tailscale mesh."""

    def __init__(self):
        self._devices: Dict[str, Device] = {}
        # Guards the in-memory map + the file write against concurrent mutation
        # (set_primary_device clears-then-sets across devices — must be atomic).
        # Reentrant so a method may nest a helper that also takes the lock.
        self._lock = threading.RLock()
        self._load_from_file()
        self._dedupe_primaries()

    def _load_from_file(self):
        """Load devices from devices.json.

        Each row is parsed in isolation: a malformed / unknown-enum row (e.g. a future
        ``device_type: "tv"`` this build doesn't know) is SKIPPED + logged rather than
        aborting the whole registry load, so one bad row can't strand every device.
        """
        if DEVICES_FILE.exists():
            with open(DEVICES_FILE) as f:
                data = json.load(f)
            for row in data.get("devices", []):
                try:
                    device = Device.from_dict(row)
                except Exception as e:
                    rid = row.get("id", "?") if isinstance(row, dict) else "?"
                    print(f"[DEVICE REGISTRY] Skipping malformed/unknown device row "
                          f"{rid!r}: {e}")
                    continue
                self._devices[device.id] = device
        print(f"[DEVICE REGISTRY] Loaded {len(self._devices)} devices")

    def _dedupe_primaries(self):
        """Defensive: enforce the at-most-one-primary-per-owner invariant on load.

        A hand-edited devices.json could mark two devices of one owner primary;
        keep the first seen, clear the rest. Persists only if it had to fix
        something (avoids a needless write on every boot).
        """
        seen_primary_owners = set()
        changed = False
        for d in self._devices.values():
            if not d.is_primary:
                continue
            key = d.owner.lower()
            if key in seen_primary_owners:
                d.is_primary = False
                changed = True
            else:
                seen_primary_owners.add(key)
        if changed:
            self._save_to_file()

    def _save_to_file(self):
        """Persist devices to devices.json atomically (unique tmp write + os.replace).

        os.replace is atomic on POSIX, so a crash mid-write can never leave a
        truncated/corrupt registry — readers see either the old or the new file.
        The tmp file is created with ``tempfile.mkstemp`` in the SAME directory
        (unique name → multi-process/writer safe, no fixed ``.tmp`` collision) and the
        directory itself is fsync'd after the rename so the swap is crash-durable.
        Serialized by ``self._lock`` so a concurrent set_primary can't interleave.
        """
        with self._lock:
            data = {"devices": [d.to_dict() for d in self._devices.values()]}
            dir_path = DEVICES_FILE.parent
            fd, tmp_name = tempfile.mkstemp(
                dir=str(dir_path), prefix=".devices-", suffix=".json.tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, DEVICES_FILE)
                # fsync the directory so the rename (not just the file bytes) survives
                # a crash. Best-effort — some filesystems reject a dir fsync.
                try:
                    dir_fd = os.open(str(dir_path), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            except Exception:
                # Never leave an orphaned tmp file behind on a failed write.
                try:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def get_device(self, device_id: str) -> Optional[Device]:
        return self._devices.get(device_id)

    def get_all_devices(self) -> List[Device]:
        return list(self._devices.values())

    def get_devices_by_type(self, device_type: DeviceType) -> List[Device]:
        return [d for d in self._devices.values() if d.device_type == device_type]

    def get_devices_by_protocol(self, protocol: DeviceProtocol) -> List[Device]:
        return [d for d in self._devices.values() if d.protocol == protocol]

    def get_devices_by_owner(self, owner: str) -> List[Device]:
        return [d for d in self._devices.values()
                if d.owner.lower() == owner.lower()]

    def get_android_devices(self) -> List[Device]:
        return self.get_devices_by_type(DeviceType.ANDROID)

    def get_desktop_devices(self) -> List[Device]:
        return [d for d in self._devices.values()
                if d.device_type in (DeviceType.LINUX, DeviceType.WINDOWS, DeviceType.MACOS)]

    def add_device(self, device: Device) -> Device:
        self._devices[device.id] = device
        self._save_to_file()
        print(f"[DEVICE REGISTRY] Added device: {device.id} ({device.name})")
        return device

    def remove_device(self, device_id: str) -> bool:
        if device_id in self._devices:
            del self._devices[device_id]
            self._save_to_file()
            print(f"[DEVICE REGISTRY] Removed device: {device_id}")
            return True
        return False

    def update_device(self, device_id: str, **kwargs) -> Optional[Device]:
        device = self._devices.get(device_id)
        if not device:
            return None
        for key, value in kwargs.items():
            if hasattr(device, key):
                setattr(device, key, value)
        self._save_to_file()
        return device

    # ── M3: primary-device + default-provider (origin-aware routing support) ──

    def get_primary_device(self, owner: str) -> Optional[Device]:
        """The owner's designated primary (default control target), or None.

        Matching is case-insensitive on owner. If (defensively) more than one is
        flagged, the first is returned deterministically — but the setter + the
        load-time dedupe keep it to exactly one.
        """
        if not owner:
            return None
        key = owner.lower()
        for d in self._devices.values():
            if d.owner.lower() == key and d.is_primary:
                return d
        return None

    def set_primary_device(self, owner: str, device_id: str) -> Optional[Device]:
        """Designate ``device_id`` the owner's primary, atomically clearing any
        prior primary of that owner, then persist. Returns the new primary, or
        None if the device does not exist or is not owned by ``owner``
        (operator-isolation: you cannot make another operator's device your
        primary). The clear-then-set + write happen under the lock so a racing
        writer can never observe (or persist) two primaries.
        """
        with self._lock:
            target = self._devices.get(device_id)
            if not target or target.owner.lower() != (owner or "").lower():
                return None
            for d in self._devices.values():
                if d.owner.lower() == owner.lower():
                    d.is_primary = (d.id == device_id)
            self._save_to_file()
            print(f"[DEVICE REGISTRY] Primary for {owner} -> {device_id}")
            return target

    def get_default_provider(self, device_id: str) -> Optional[str]:
        """The device's per-device default frontier provider, or None."""
        device = self._devices.get(device_id)
        return device.default_provider if device else None

    def set_default_provider(self, device_id: str, provider: Optional[str]) -> Optional[Device]:
        """Set a device's default provider (gemma|gemini|claude|openai) or clear it
        (None/""). Persists. Returns the device, or None if it does not exist.
        Raises ValueError on a non-empty but INVALID provider so the API surfaces a
        422 rather than silently swallowing a typo.
        """
        device = self._devices.get(device_id)
        if not device:
            return None
        if provider is None or (isinstance(provider, str) and not provider.strip()):
            device.default_provider = None
        else:
            normalized = sanitize_default_provider(provider)
            if normalized is None:
                raise ValueError(
                    f"invalid default_provider {provider!r}; must be one of "
                    f"{sorted(VALID_DEFAULT_PROVIDERS)} or null")
            device.default_provider = normalized
        self._save_to_file()
        return device

    async def check_device_health(self, device_id: str) -> DeviceStatus:
        """Check if a device is reachable. Uses TCP probe for ADB devices
        (ICMP ping requires cap_net_raw which systemd strips), falls back
        to ping for other protocols."""
        device = self._devices.get(device_id)
        if not device:
            return DeviceStatus.UNKNOWN
        if device.protocol == DeviceProtocol.LOCAL:
            device.status = DeviceStatus.ONLINE
            device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return DeviceStatus.ONLINE

        # For ADB devices: use TCP probe (works without cap_net_raw)
        if device.protocol == DeviceProtocol.ADB:
            try:
                from Orchestrator.adb import get_adb_manager
                mgr = get_adb_manager()
                reachable = await mgr.check_reachability(device.tailscale_ip, timeout=3)
                if reachable:
                    # Also check if actually ADB-connected
                    connected = await mgr.is_connected(device_id)
                    device.status = DeviceStatus.ONLINE
                    device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    device.metadata["adb_connected"] = connected
                else:
                    device.status = DeviceStatus.OFFLINE
                    device.metadata["adb_connected"] = False
                return device.status
            except Exception as e:
                print(f"[HEALTH] ADB health check failed for {device_id}: {e}")
                device.status = DeviceStatus.UNKNOWN
                return device.status

        # For non-ADB devices: try TCP probe first, fall back to ping
        try:
            for port in (22, 9091, 5900, 7):
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(device.tailscale_ip, port), timeout=3
                    )
                    writer.close()
                    await writer.wait_closed()
                    device.status = DeviceStatus.ONLINE
                    device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    return DeviceStatus.ONLINE
                except ConnectionRefusedError:
                    device.status = DeviceStatus.ONLINE
                    device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    return DeviceStatus.ONLINE
                except (asyncio.TimeoutError, OSError):
                    continue
            device.status = DeviceStatus.OFFLINE
        except Exception:
            device.status = DeviceStatus.UNKNOWN
        return device.status

    async def check_all_health(self) -> Dict[str, DeviceStatus]:
        """Check health of all devices in parallel."""
        tasks = {did: self.check_device_health(did) for did in self._devices}
        results = {}
        for did, coro in tasks.items():
            results[did] = await coro
        return results

    async def sync_from_tailscale(self) -> Dict[str, str]:
        """Discover devices from Tailscale network and add/update registry.

        Returns dict of {device_id: "added"|"updated"|"skipped"} for each peer.
        """
        import subprocess
        import tempfile

        # Save to temp file since piping may truncate
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                raise RuntimeError(f"tailscale status failed: {stderr.decode()}")

            data = json.loads(stdout.decode())
        except Exception as e:
            raise RuntimeError(f"Failed to get Tailscale status: {e}")

        results = {}

        # Process self node — this is always the local machine
        self_node = data.get("Self", {})
        if self_node:
            self_ips = self_node.get("TailscaleIPs", [])
            self_ipv4 = next((ip for ip in self_ips if "." in ip), "127.0.0.1")
            # Update blackbox device with real Tailscale IP and status
            blackbox = self.get_device("blackbox")
            if blackbox:
                if blackbox.tailscale_ip == "127.0.0.1":
                    blackbox.tailscale_ip = self_ipv4
                blackbox.metadata["tailscale_hostname"] = self_node.get("HostName", "")
                blackbox.metadata["tailscale_dns"] = self_node.get("DNSName", "")
                # Self node is always online — it's this machine
                blackbox.status = DeviceStatus.ONLINE
                results["blackbox"] = "updated"

        # Process peers
        peers = data.get("Peer", {})
        for key, peer in peers.items():
            hostname = peer.get("HostName", "")
            os_name = peer.get("OS", "")
            online = peer.get("Online", False)
            dns_name = peer.get("DNSName", "")
            ips = peer.get("TailscaleIPs", [])
            ipv4 = next((ip for ip in ips if "." in ip), None)

            # Skip funnel ingress nodes and peers without IPs
            if not ipv4 or hostname == "funnel-ingress-node":
                continue
            if not os_name:  # Skip nodes with no OS (funnel nodes)
                continue

            # Use DNS name for device ID (unique per device).
            # Hostname can be "localhost" for all Android devices.
            # DNS: "samsung-sm-f956u-1.tail401fb3.ts.net." → "samsung-sm-f956u-1"
            dns_slug = dns_name.split(".")[0] if dns_name else ""
            if dns_slug and dns_slug != "localhost":
                device_id = dns_slug.lower()
            elif hostname and hostname != "localhost":
                device_id = hostname.lower().replace(" ", "-")
            else:
                # Fallback: use IP as identifier
                device_id = f"device-{ipv4.replace('.', '-')}"

            # Determine device type and protocol
            os_lower = os_name.lower()
            if os_lower == "android":
                device_type = DeviceType.ANDROID
                protocol = DeviceProtocol.ADB
            elif os_lower == "linux":
                device_type = DeviceType.LINUX
                protocol = DeviceProtocol.VNC
            elif os_lower == "windows":
                device_type = DeviceType.WINDOWS
                protocol = DeviceProtocol.VNC
            elif os_lower in ("macos", "ios"):
                device_type = DeviceType.MACOS
                protocol = DeviceProtocol.VNC
            else:
                device_type = DeviceType.LINUX
                protocol = DeviceProtocol.VNC

            # Generate friendly name from DNS slug or hostname
            name_source = dns_slug if dns_slug and dns_slug != "localhost" else hostname
            friendly_name = name_source.replace("-", " ").title()

            # Check if device already exists
            existing = self.get_device(device_id)
            if existing:
                # Update IP and online status
                existing.tailscale_ip = ipv4
                existing.status = DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE
                existing.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if online else existing.last_seen
                existing.metadata["tailscale_hostname"] = hostname
                existing.metadata["tailscale_dns"] = dns_name
                existing.metadata["tailscale_online"] = online
                results[device_id] = "updated"
            else:
                # Create new device
                device = Device(
                    id=device_id,
                    name=friendly_name,
                    tailscale_ip=ipv4,
                    device_type=device_type,
                    protocol=protocol,
                    owner="",  # I2: discovery is UNCLAIMED — ownership set only via POST /{id}/operator
                    description=f"Auto-discovered from Tailscale ({os_name})",
                    status=DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE,
                    last_seen=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if online else None,
                    metadata={
                        "tailscale_hostname": hostname,
                        "tailscale_dns": dns_name,
                        "tailscale_online": online,
                        "auto_discovered": True,
                    }
                )
                self._devices[device_id] = device
                results[device_id] = "added"
                print(f"[DEVICE REGISTRY] Auto-discovered: {device_id} ({friendly_name}) at {ipv4}")

        self._save_to_file()
        return results

    def to_prompt_context(self, owner: Optional[str] = None) -> str:
        """Generate a text summary for injection into AI system prompts."""
        devices = self.get_devices_by_owner(owner) if owner else self.get_all_devices()
        if not devices:
            return "No devices registered in the device registry."
        lines = ["Available devices on the Tailscale mesh network:"]
        for d in devices:
            status = f" [{d.status.value}]" if d.status != DeviceStatus.UNKNOWN else ""
            lines.append(
                f"  - {d.id}: {d.name} | Type: {d.device_type.value} | "
                f"Protocol: {d.protocol.value} | IP: {d.tailscale_ip}{status}"
            )
            if d.description:
                lines.append(f"    Description: {d.description}")
        return "\n".join(lines)


# ── Singleton ──
_registry: Optional[DeviceRegistry] = None

def get_registry() -> DeviceRegistry:
    global _registry
    if _registry is None:
        _registry = DeviceRegistry()
    return _registry
