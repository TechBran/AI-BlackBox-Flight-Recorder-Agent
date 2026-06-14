"""Local provider package — operator-bound on-device model attestation."""
from .registry import get_local_registry, LocalProviderRegistry

__all__ = ["get_local_registry", "LocalProviderRegistry"]
