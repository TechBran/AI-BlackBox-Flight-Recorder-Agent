from Orchestrator import local_stack


def test_speaches_static_port_is_9099():
    assert local_stack.SPEACHES_STATIC_PORT == 9099


def test_front_door_strips_v1(monkeypatch):
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    assert local_stack.front_door() == "http://127.0.0.1:9098"


def test_warm_url_hits_upstream_speaches_health(monkeypatch):
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    assert local_stack.speaches_warm_url() == "http://127.0.0.1:9098/upstream/speaches/health"


def test_realtime_ws_url_is_direct_to_9099():
    url = local_stack.speaches_realtime_ws_url("deepdml/faster-whisper-large-v3-turbo-ct2")
    assert url.startswith("ws://127.0.0.1:9099/v1/realtime?")
    assert "model=deepdml%2Ffaster-whisper-large-v3-turbo-ct2" in url
    assert "intent=transcription" in url


def test_stt_model_getters():
    assert local_stack.stt_stream_model() == "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert local_stack.stt_batch_model() == "Systran/faster-whisper-large-v3"
