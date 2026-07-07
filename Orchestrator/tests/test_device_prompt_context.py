"""M5.2: to_prompt_context reflects the "unclaimed until assigned" product story.

M1.1 made Tailscale discovery create UNCLAIMED devices (``owner=""``). This test pins the
downstream consequence for AI system-prompt injection (``registry.to_prompt_context``):
  * a device CLAIMED by operator X appears in ``to_prompt_context(owner="X")``,
  * an UNCLAIMED device (``owner=""``) does NOT appear in that owner-scoped context —
    ``get_devices_by_owner`` returns ``[]`` for a blank owner (a blank owner is the
    UNCLAIMED sentinel, never a query key), so an unclaimed node is never attributed to
    someone else's prompt context,
  * ``to_prompt_context(owner=None)`` (no owner → unscoped) includes ALL devices, both
    claimed and unclaimed.

Hermetic + isolated: monkeypatch ``registry.DEVICES_FILE`` to a tmp file, reset the
singleton, and point the legacy-migration seam at a non-existent tmp path. The live
``Orchestrator/device_registry/devices.json`` is NEVER touched.
"""
import pytest

import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A fresh file-backed DeviceRegistry with one CLAIMED and one UNCLAIMED device."""
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.DeviceRegistry()
    # CLAIMED by Casey.
    r.add_device(Device(id="casey-phone", name="Casey Phone", tailscale_ip="100.70.0.1",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner="Casey"))
    # UNCLAIMED (owner="") — the freshly-discovered, not-yet-claimed case.
    r.add_device(Device(id="lobby-tablet", name="Lobby Tablet", tailscale_ip="100.70.0.2",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner=""))
    return r


def test_owner_scoped_context_includes_claimed_device(registry):
    # A device CLAIMED by Casey appears in Casey's device context.
    ctx = registry.to_prompt_context(owner="Casey")
    assert "casey-phone" in ctx


def test_owner_scoped_context_excludes_unclaimed_device(registry):
    # An UNCLAIMED device must NOT leak into an operator's scoped context.
    ctx = registry.to_prompt_context(owner="Casey")
    assert "lobby-tablet" not in ctx


def test_unscoped_context_includes_all_devices(registry):
    # No owner → unscoped: BOTH the claimed and the unclaimed device are listed.
    ctx = registry.to_prompt_context(owner=None)
    assert "casey-phone" in ctx
    assert "lobby-tablet" in ctx
