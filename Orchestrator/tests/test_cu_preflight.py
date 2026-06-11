"""CU preflight — machine-readiness checks with remediation strings."""
from unittest.mock import patch

from Orchestrator.browser import preflight


def test_check_shape():
    """Every check returns the locked shape."""
    report = preflight.run_preflight(skip_screenshot=True)
    assert isinstance(report["checks"], list) and report["checks"]
    for c in report["checks"]:
        assert set(c) >= {"id", "status", "detail", "remediation"}
        assert c["status"] in ("ok", "warn", "fail")
    assert report["status"] in ("ok", "warn", "fail")


def test_input_backend_wayland_no_ydotool(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight.shutil, "which",
                        lambda b: None if b == "ydotool" else f"/usr/bin/{b}")
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "ydotool" in c["remediation"]


def test_input_backend_wayland_daemon_dead(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(preflight, "_ydotool_socket_alive", lambda: False)
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "daemon" in c["remediation"].lower()


def test_x11_xdotool_ok(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: False)
    monkeypatch.setattr(preflight.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert preflight.check_input_backend()["status"] == "ok"


def test_api_keys_reported(monkeypatch):
    monkeypatch.setattr(preflight, "ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setattr(preflight, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(preflight, "OPENAI_API_KEY", "")
    c = preflight.check_api_keys()
    assert c["status"] == "warn"          # at least one backend usable
    assert "google" in c["detail"].lower()
