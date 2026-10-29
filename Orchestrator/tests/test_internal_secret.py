# Orchestrator/tests/test_internal_secret.py
"""Per-boot shared secret for MCP POST /internal/reload (audit M3 U20 item 3,
O20-mcp-server-08).

The MCP server trusted ``request.client.host == 127.0.0.1``, but Tailscale
Funnel delivers INTERNET requests to 127.0.0.1:9093 (and tailscale serve
delivers every tailnet request from loopback), so "the caller is loopback"
proves nothing. The reload now needs a secret only processes running as the
service user can read: credentials/mcp_reload.secret, 0600, bound to this
boot, written by whichever side needs it first, compared in constant time.

Hermetic: tmp paths, a fake boot id, a faked httpx.post.
"""
import os
import stat
import threading

import pytest

from Orchestrator.utils import internal_secret as isec


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    monkeypatch.setattr(isec, "_boot_id", lambda: "boot-A")
    return tmp_path / "credentials" / "mcp_reload.secret"


def test_ensure_creates_a_private_random_secret(secret_file):
    s = isec.ensure_secret(secret_file)
    assert len(s) >= 32
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert isec.ensure_secret(secret_file) == s          # stable within a boot
    assert isec.read_secret(secret_file) == s


def test_new_boot_rotates_the_secret(secret_file, monkeypatch):
    first = isec.ensure_secret(secret_file)
    monkeypatch.setattr(isec, "_boot_id", lambda: "boot-B")
    assert isec.read_secret(secret_file) is None          # last boot's secret is dead
    second = isec.ensure_secret(secret_file)
    assert second != first
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


def test_verify_accepts_only_the_current_secret(secret_file):
    s = isec.ensure_secret(secret_file)
    assert isec.verify(s, secret_file) is True
    for bad in ("", None, s + "x", s[:-1], "nope", s.upper() if s.upper() != s else s + "!"):
        assert isec.verify(bad, secret_file) is False, bad


def test_verify_fails_closed_without_a_file(secret_file):
    assert not secret_file.exists()
    assert isec.verify("anything", secret_file) is False
    assert isec.verify("", secret_file) is False
    assert not secret_file.exists()                       # verify never creates one


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"boot_id": "boot-A"}',
                                     '{"boot_id": "boot-A", "secret": ""}',
                                     '{"boot_id": "boot-A", "secret": 7}'])
def test_corrupt_file_is_not_a_secret(secret_file, content):
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(content)
    assert isec.read_secret(secret_file) is None
    assert isec.verify("", secret_file) is False
    fresh = isec.ensure_secret(secret_file)               # and gets replaced
    assert fresh and isec.verify(fresh, secret_file)


def test_a_group_or_world_readable_file_is_not_trusted(secret_file):
    s = isec.ensure_secret(secret_file)
    os.chmod(secret_file, 0o644)
    assert isec.read_secret(secret_file) is None
    assert isec.verify(s, secret_file) is False


def test_concurrent_first_writers_agree(secret_file):
    got, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        got.append(isec.ensure_secret(secret_file))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1
    assert isec.verify(got[0], secret_file)


def test_default_path_is_under_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", raising=False)
    assert isec.mcp_reload_secret_path(tmp_path) == tmp_path / "credentials" / "mcp_reload.secret"
    monkeypatch.setenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", str(tmp_path / "x.secret"))
    assert isec.mcp_reload_secret_path(tmp_path) == tmp_path / "x.secret"


# ------------------------------------------- the Orchestrator caller (mcp_routes)

def test_trigger_reload_sends_the_secret(secret_file, monkeypatch):
    from Orchestrator.routes import mcp_routes

    monkeypatch.setenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", str(secret_file))
    sent = {}

    class _R:
        status_code = 200

    def fake_post(url, headers=None, **kw):
        sent.update(url=url, headers=headers or {})
        return _R()

    monkeypatch.setattr(mcp_routes.httpx, "post", fake_post)
    assert mcp_routes._trigger_reload() is True
    assert sent["url"].endswith("/internal/reload")
    assert isec.verify(sent["headers"].get(isec.SECRET_HEADER), secret_file)


def test_trigger_reload_is_best_effort_when_the_secret_cannot_be_written(monkeypatch, tmp_path):
    from Orchestrator.routes import mcp_routes

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", str(blocker / "mcp_reload.secret"))
    monkeypatch.setattr(mcp_routes.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no secret, no call")))
    assert mcp_routes._trigger_reload() is False


# ------------------------------------- the loopback control listener (O20-08)

def test_trigger_reload_goes_to_the_loopback_control_listener(secret_file, monkeypatch):
    """The reload (and its secret) goes to the MCP server's control listener,
    never to the :9093 listener Tailscale Funnel forwards the internet to."""
    from Orchestrator.routes import mcp_routes

    monkeypatch.setenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("BLACKBOX_MCP_CONTROL_PORT", raising=False)
    urls = []

    class _R:
        status_code = 200

    def fake_post(url, headers=None, **kw):
        urls.append(url)
        return _R()

    monkeypatch.setattr(mcp_routes.httpx, "post", fake_post)
    assert mcp_routes._trigger_reload() is True
    monkeypatch.setenv("BLACKBOX_MCP_CONTROL_PORT", "19094")
    assert mcp_routes._trigger_reload() is True
    assert urls == ["http://127.0.0.1:9094/internal/reload",
                    "http://127.0.0.1:19094/internal/reload"]
    assert not any(":9093" in u or u.startswith(mcp_routes.MCP_SERVER_URL) for u in urls)


@pytest.mark.parametrize("raw,port", [("", 9094), ("9094", 9094), ("0", 0), ("19094", 19094),
                                      ("70000", 9094), ("-1", 9094), ("nope", 9094),
                                      (" 8123 ", 8123)])
def test_control_port_parsing(monkeypatch, raw, port):
    monkeypatch.setenv("BLACKBOX_MCP_CONTROL_PORT", raw)
    assert isec.mcp_control_port() == port
    assert isec.mcp_control_url() == f"http://127.0.0.1:{port}"
