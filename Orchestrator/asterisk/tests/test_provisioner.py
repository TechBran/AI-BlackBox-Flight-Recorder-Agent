"""Unit tests for the Asterisk config provisioner.

These tests are HERMETIC and SAFE:
- All file I/O is redirected to a pytest ``tmp_path`` via the
  ``ASTERISK_INCLUDE_DIR`` env override (monkeypatched onto
  ``provisioner.BLACKBOX_D``). The real ``/etc/asterisk`` is never touched.
- ``provisioner.subprocess.run`` is monkeypatched to a fake that records
  invocations. The real ``asterisk -rx ... reload`` is NEVER executed.
"""

import os

import pytest

from Orchestrator.asterisk import provisioner
from Orchestrator.asterisk.config import ASTERISK_AUDIOSOCKET_PORT


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _gw():
    """A representative v2 gateway dict."""
    return {
        "id": "abcd1234",
        "name": "TG200 Lab",
        "model": "TG200",
        "ip": "192.168.5.150",
        "sip_port": 5060,
        "codec": "g722",
        "trunk_name": "tg-tg200-lab",
        "ports": [],
    }


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def include_dir(tmp_path, monkeypatch):
    """Redirect BLACKBOX_D at the tmp dir so no real path is written."""
    monkeypatch.setenv("ASTERISK_INCLUDE_DIR", str(tmp_path))
    monkeypatch.setattr(provisioner, "BLACKBOX_D", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_run(monkeypatch):
    """Record every subprocess.run call; never execute anything."""
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "args": args, "kwargs": kwargs})
        return _FakeProc(returncode=0)

    monkeypatch.setattr(provisioner.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# render_pjsip
# ---------------------------------------------------------------------------
def test_render_pjsip_contains_trunk_ip_codec():
    out = provisioner.render_pjsip(_gw())
    assert "tg-tg200-lab" in out
    assert "192.168.5.150" in out
    assert "g722" in out


# ---------------------------------------------------------------------------
# render_shared_dialplan
# ---------------------------------------------------------------------------
def test_render_shared_dialplan_contents():
    out = provisioner.render_shared_dialplan()
    assert "from-tg200" in out
    assert "Stasis(blackbox" in out
    assert "AudioSocket(" in out
    assert str(ASTERISK_AUDIOSOCKET_PORT) in out
    assert "blackbox-audiosocket" in out
    # Outbound context is implicit but included as a minimal convenience.
    assert "to-tg200" in out


# ---------------------------------------------------------------------------
# write_gateway_config
# ---------------------------------------------------------------------------
def test_write_gateway_config_creates_files(include_dir):
    gw = _gw()
    path = provisioner.write_gateway_config(gw)

    per_gw = os.path.join(str(include_dir), f"{gw['trunk_name']}.conf")
    shared = os.path.join(str(include_dir), "_shared.conf")

    assert path == per_gw
    assert os.path.exists(per_gw)
    assert os.path.exists(shared)

    content = open(per_gw).read()
    # Per-gateway file holds the PJSIP block (endpoint/aor/identify).
    assert f"[{gw['trunk_name']}]" in content
    assert "type=endpoint" in content
    assert gw["ip"] in content

    shared_content = open(shared).read()
    assert "from-tg200" in shared_content
    assert "blackbox-audiosocket" in shared_content


def test_write_gateway_config_idempotent(include_dir):
    gw = _gw()
    provisioner.write_gateway_config(gw)
    # Second call must not raise and must not duplicate files.
    provisioner.write_gateway_config(gw)

    confs = [f for f in os.listdir(str(include_dir)) if f.endswith(".conf")]
    assert sorted(confs) == ["_shared.conf", f"{gw['trunk_name']}.conf"]


# ---------------------------------------------------------------------------
# remove_gateway_config
# ---------------------------------------------------------------------------
def test_remove_gateway_config(include_dir):
    gw = _gw()
    provisioner.write_gateway_config(gw)

    assert provisioner.remove_gateway_config(gw["trunk_name"]) is True
    assert not os.path.exists(
        os.path.join(str(include_dir), f"{gw['trunk_name']}.conf")
    )
    # Already gone -> False.
    assert provisioner.remove_gateway_config(gw["trunk_name"]) is False


# ---------------------------------------------------------------------------
# reload_asterisk
# ---------------------------------------------------------------------------
def test_reload_asterisk_ok(fake_run, monkeypatch):
    monkeypatch.setattr(provisioner, "ASTERISK_BIN", "/usr/sbin/asterisk")
    result = provisioner.reload_asterisk()

    assert result["ok"] is True
    assert len(fake_run) == 2

    # Both invocations include the configured binary path.
    flat0 = " ".join(fake_run[0]["cmd"])
    flat1 = " ".join(fake_run[1]["cmd"])
    assert "/usr/sbin/asterisk" in flat0
    assert "/usr/sbin/asterisk" in flat1
    assert "pjsip reload" in flat0
    assert "dialplan reload" in flat1


def test_reload_asterisk_filenotfound(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("asterisk: command not found")

    monkeypatch.setattr(provisioner.subprocess, "run", _boom)
    result = provisioner.reload_asterisk()

    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# apply_gateway
# ---------------------------------------------------------------------------
def test_apply_gateway(include_dir, fake_run):
    gw = _gw()
    result = provisioner.apply_gateway(gw)

    assert result["written"] == os.path.join(
        str(include_dir), f"{gw['trunk_name']}.conf"
    )
    assert result["reload"]["ok"] is True
