import pytest

from ugv_tools_api.supervisor import config as cfg

# UGV Beast sub-project (tests/supervisor/conftest.py): opt-in, -m ugv.
pytestmark = pytest.mark.ugv


def test_defaults(monkeypatch):
    for v in ("SUPERVISOR_MODEL", "SUPERVISOR_VOICE", "SUPERVISOR_LANG",
              "SUPERVISOR_MIC", "SUPERVISOR_SPK", "TOOLS_API_URL", "ER_URL",
              "SUPERVISOR_CAMERA_TOPIC", "SUPERVISOR_COSTMAP_TOPIC",
              "SUPERVISOR_HANDLE_STORE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    s = cfg.load()
    assert s.model == "gemini-2.5-flash-native-audio-latest"
    assert s.voice == "Orus"
    assert s.language_code == "en-US"
    assert s.mic_device == "pulse"
    assert s.spk_device == "pulse"
    assert s.tools_api_url == "http://localhost:8080"
    assert s.er_url == "http://localhost:8082"
    assert s.camera_topic == "/camera/image/compressed"
    assert s.costmap_topic == "/global_costmap/costmap"
    assert s.handle_store_path == "/var/lib/ugv_supervisor/session_handle.txt"


def test_override_via_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("SUPERVISOR_VOICE", "Charon")
    monkeypatch.setenv("TOOLS_API_URL", "http://10.0.0.5:8080")
    s = cfg.load()
    assert s.voice == "Charon"
    assert s.tools_api_url == "http://10.0.0.5:8080"


def test_google_api_key_required(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        cfg.load()


def test_google_api_key_whitespace_rejected(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        cfg.load()
