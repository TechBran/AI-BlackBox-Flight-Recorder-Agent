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
    """Binary missing → remediation points at the BlackBox installer, never apt."""
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight, "_ydotool_available", lambda: False)
    monkeypatch.setattr(preflight.os.path, "isfile", lambda p: False)
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "Scripts/install.sh" in c["remediation"]
    assert "apt install ydotool" not in c["remediation"]


def test_input_backend_wayland_daemon_dead(monkeypatch):
    """Binary present, daemon socket unusable → remediation names ydotoold unit."""
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight, "_ydotool_available", lambda: False)
    monkeypatch.setattr(preflight.os.path, "isfile", lambda p: True)
    c = preflight.check_input_backend()
    assert c["status"] == "fail"
    assert "ydotoold" in c["remediation"]


def test_input_backend_wayland_ok(monkeypatch):
    monkeypatch.setattr(preflight, "_is_wayland", lambda: True)
    monkeypatch.setattr(preflight, "_ydotool_available", lambda: True)
    assert preflight.check_input_backend()["status"] == "ok"


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


def _canned(id_, status):
    return {"id": id_, "status": status, "detail": "canned", "remediation": ""}


_CHECK_NAMES = ["check_display", "check_input_backend", "check_resolution",
                "check_api_keys", "check_chrome", "check_remote_tools"]


def test_aggregation_precedence(monkeypatch):
    """Aggregate status is the worst individual status: fail > warn > ok."""
    def set_all(statuses):
        for name, status in zip(_CHECK_NAMES, statuses):
            monkeypatch.setattr(
                preflight, name,
                lambda *a, _n=name, _s=status, **kw: _canned(_n, _s))

    set_all(["ok"] * 6)
    assert preflight.run_preflight(skip_screenshot=True)["status"] == "ok"

    set_all(["ok", "warn", "ok", "ok", "ok", "ok"])
    assert preflight.run_preflight(skip_screenshot=True)["status"] == "warn"

    set_all(["ok", "warn", "ok", "fail", "ok", "warn"])
    assert preflight.run_preflight(skip_screenshot=True)["status"] == "fail"


def test_raising_check_degrades(monkeypatch):
    """A check that raises degrades to a fail entry; the report still answers."""
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(preflight, "check_display", boom)
    report = preflight.run_preflight(skip_screenshot=True)
    display = next(c for c in report["checks"] if c["id"] == "display")
    assert display["status"] == "fail"
    assert "boom" in display["detail"]
    assert report["status"] == "fail"


def test_preflight_route():
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    with patch.object(preflight, "run_preflight",
                      return_value={"status": "ok", "checks": []}) as m:
        client = TestClient(app)
        r = client.get("/cu/preflight")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
