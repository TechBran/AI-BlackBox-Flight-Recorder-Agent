"""Offline unit tests for the voice-probe harness (diagnostics/voice_probes/).

Pure helpers only — no network. Live probes live in
diagnostics/voice_probes/test_live_probes.py (marker: probe_live), which sits
OUTSIDE pytest.ini's testpaths so the default suite never dials a provider.
"""
import json

from diagnostics.voice_probes.env import load_service_env
from diagnostics.voice_probes.harness import (
    ProbeResult,
    build_gemini_url,
    build_openai_url,
    build_xai_url,
    classify_first_event,
    truncate_deep,
    write_results,
)


def test_load_service_env_parses_and_strips(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "OPENAI_API_KEY=sk-aaa111\n"
        'XAI_API_KEY="xai-bbb222"\n'
        "EMPTY=\n"
        "not a kv line\n"
    )
    env = load_service_env(env_file)
    assert env["OPENAI_API_KEY"] == "sk-aaa111"
    assert env["XAI_API_KEY"] == "xai-bbb222"  # quotes stripped
    assert env["EMPTY"] == ""
    assert "not a kv line" not in env


def test_load_service_env_missing_file_is_empty(tmp_path):
    assert load_service_env(tmp_path / "nope.env") == {}


def test_url_builders():
    assert build_openai_url("gpt-realtime-2.1") == (
        "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    )
    assert build_xai_url() == "wss://api.x.ai/v1/realtime"
    assert build_xai_url("grok-voice-latest") == (
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    )
    url = build_gemini_url("v1alpha", "SEKRETKEY123")
    assert url.startswith(
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
    )
    assert url.endswith("?key=SEKRETKEY123")


def test_classify_first_event():
    ok, resolved = classify_first_event(
        "xai",
        {"type": "session.created", "session": {"model": "grok-voice-think-fast-1.0"}},
    )
    assert ok and resolved == "grok-voice-think-fast-1.0"
    ok, _ = classify_first_event("openai", {"type": "error", "error": {"message": "unknown model"}})
    assert not ok
    ok, _ = classify_first_event("gemini", {"setupComplete": {}})
    assert ok
    ok, _ = classify_first_event("gemini", {"serverContent": {}})
    assert not ok


def test_truncate_deep_caps_long_strings():
    obj = {"audio": "A" * 5000, "nested": [{"delta": "B" * 5000}], "n": 7}
    out = truncate_deep(obj, max_str=300)
    assert len(out["audio"]) < 400 and "truncated 5000" in out["audio"]
    assert "truncated 5000" in out["nested"][0]["delta"]
    assert out["n"] == 7


def test_write_results_redacts_secrets(tmp_path):
    r = ProbeResult(
        provider="gemini", model="m", probe="handshake",
        error="HTTP 403 for url ?key=SEKRETKEY123",
    )
    path = write_results("unit", [r], results_dir=tmp_path, secrets=["SEKRETKEY123"])
    text = path.read_text()
    assert "SEKRETKEY123" not in text
    assert "***REDACTED***" in text
    payload = json.loads(text)
    assert payload["results"][0]["probe"] == "handshake"
    assert path.name.endswith("-unit.json")  # date-stamped prefix


def test_probe_result_event_cap_and_summary():
    r = ProbeResult(
        provider="xai", model="", probe="handshake", ok=True,
        resolved_model="grok-voice-think-fast-1.0",
    )
    for i in range(200):
        r.add_event({"type": f"e{i}"})
    assert len(r.events) == 60  # MAX_EVENTS cap
    s = r.summary()
    assert "(default)" in s and "resolved=grok-voice-think-fast-1.0" in s and "OK" in s


def test_service_secrets_includes_process_env(tmp_path, monkeypatch):
    # os.environ divergence must still be swept (P0.1 review issue #1)
    monkeypatch.setenv("ROTATED_API_KEY", "envonlysecret99")
    from diagnostics.voice_probes import harness
    monkeypatch.setattr(harness, "load_service_env", lambda: {"OLD_API_KEY": "fileonlysecret1"})
    secrets = harness.service_secrets()
    assert "envonlysecret99" in secrets
    assert "fileonlysecret1" in secrets
    # same NAME in both sources with different values: BOTH must sweep (rotated key)
    monkeypatch.setenv("SHARED_API_KEY", "newrotated99")
    monkeypatch.setattr(harness, "load_service_env",
                        lambda: {"OLD_API_KEY": "fileonlysecret1", "SHARED_API_KEY": "oldrotated88"})
    secrets = harness.service_secrets()
    assert "newrotated99" in secrets and "oldrotated88" in secrets


def test_redact_text_falsy_and_prefix_safe():
    from diagnostics.voice_probes.harness import redact_text
    # falsy entries are skipped, not applied
    assert redact_text("hello", ["", None]) == "hello"
    # longer secret redacts first so its prefix-secret leaves no fragment
    out = redact_text("token=abcdef12XYZ", ["abcdef12", "abcdef12XYZ"])
    assert out == "token=***REDACTED***"
