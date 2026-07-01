"""Device Registry — Tailscale mesh device management."""
from .registry import get_registry, DeviceRegistry
from .models import (
    Device, DeviceType, DeviceProtocol, DeviceStatus,
    VALID_DEFAULT_PROVIDERS, sanitize_default_provider,
)

__all__ = [
    "get_registry", "DeviceRegistry",
    "Device", "DeviceType", "DeviceProtocol", "DeviceStatus",
    "VALID_DEFAULT_PROVIDERS", "sanitize_default_provider",
]
