"""Device registry data models."""
from dataclasses import dataclass, field, asdict, fields
from enum import Enum
from typing import Optional, Dict, Any
import json
import logging

log = logging.getLogger(__name__)

_PORT_DEFAULTS = {"adb_port": 5555, "vnc_port": 5900, "rdp_port": 3389}

# Valid default-provider choices for a device's frontier device-control brain (M3).
# None = "no per-device preference" (fall back to the box/config default). Kept as a
# plain set (not an Enum) so config/onboarding can pass strings without a coupling.
VALID_DEFAULT_PROVIDERS = frozenset({"gemma", "gemini", "claude", "openai"})


def _sanitize_port(value: int, field_name: str) -> int:
    """Clamp port to valid range 1-65535, reset to default if out of range."""
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    default = _PORT_DEFAULTS.get(field_name, 5555)
    log.warning(f"[DeviceRegistry] Invalid {field_name}={value} — resetting to {default}")
    return default


def sanitize_default_provider(value) -> Optional[str]:
    """Normalize a default_provider to a valid lowercase choice or None.

    Blank/None/unknown values collapse to None (no per-device preference) rather
    than raising — migration-safe for hand-edited or legacy stores. Callers that
    want to REJECT an invalid provider (e.g. the API setter) validate separately.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        log.warning(f"[DeviceRegistry] Invalid default_provider={value!r} — resetting to None")
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in VALID_DEFAULT_PROVIDERS:
        log.warning(f"[DeviceRegistry] Unknown default_provider={value!r} — resetting to None")
        return None
    return v


class DeviceType(str, Enum):
    ANDROID = "android"
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"


class DeviceProtocol(str, Enum):
    ADB = "adb"           # Android Debug Bridge (for Android devices)
    LOCAL = "local"       # Local display (xdotool/scrot on this machine)
    VNC = "vnc"           # VNC remote desktop
    RDP = "rdp"           # Windows Remote Desktop


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"         # Currently being controlled by a CU session
    UNKNOWN = "unknown"


@dataclass
class Device:
    """A device on the Tailscale mesh network."""
    id: str                              # Unique slug: "brandon-phone", "work-laptop"
    name: str                            # Human-friendly: "Z Fold 6", "Work Laptop"
    tailscale_ip: str                    # 100.x.x.x (or 127.0.0.1 for local)
    device_type: DeviceType
    protocol: DeviceProtocol
    owner: str                           # Operator who owns this device: "Brandon"
    description: str = ""                # "GPU box with StoryBox", etc.
    adb_port: int = 5555                 # ADB wireless debugging port (Android only)
    vnc_port: int = 5900                 # VNC port (desktop only)
    rdp_port: int = 3389                 # RDP port (Windows only)
    status: DeviceStatus = DeviceStatus.UNKNOWN
    last_seen: Optional[str] = None      # ISO timestamp
    is_primary: bool = False             # the owner's default control target (M3)
    default_provider: Optional[str] = None  # gemma|gemini|claude|openai|None (M3)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def connection_string(self) -> str:
        """Get the connection string for this device."""
        if self.protocol == DeviceProtocol.ADB:
            return f"{self.tailscale_ip}:{self.adb_port}"
        elif self.protocol == DeviceProtocol.VNC:
            return f"{self.tailscale_ip}:{self.vnc_port}"
        elif self.protocol == DeviceProtocol.RDP:
            return f"{self.tailscale_ip}:{self.rdp_port}"
        elif self.protocol == DeviceProtocol.LOCAL:
            return "localhost"
        return self.tailscale_ip

    def to_dict(self) -> dict:
        d = asdict(self)
        d["device_type"] = self.device_type.value
        d["protocol"] = self.protocol.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        data = data.copy()
        data["device_type"] = DeviceType(data["device_type"])
        data["protocol"] = DeviceProtocol(data["protocol"])
        data["status"] = DeviceStatus(data.get("status", "unknown"))
        # Sanitize ports — prevents invalid values like 917701 from persisting
        for port_field in ("adb_port", "vnc_port", "rdp_port"):
            if port_field in data:
                data[port_field] = _sanitize_port(data[port_field], port_field)
        # M3 migration-safe: coerce is_primary + validate default_provider. Absent in
        # legacy devices.json → dataclass defaults (False / None) apply below.
        if "is_primary" in data:
            data["is_primary"] = bool(data["is_primary"])
        if "default_provider" in data:
            data["default_provider"] = sanitize_default_provider(data["default_provider"])
        # Forward/back-compat: drop any keys not on the dataclass so an OLDER Device
        # never crashes on a NEWER field, and a partial legacy row just gets defaults.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        return cls(**data)
