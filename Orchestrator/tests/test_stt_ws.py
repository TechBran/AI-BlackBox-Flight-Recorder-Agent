def test_ws_stt_dispatches_and_relays(monkeypatch):
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.routes import stt_ws_routes
    async def fake_bridge(ws, provider, start):
        await ws.send_json({"type":"stt_delta","text":"Hel","target":start.get("target")})
        await ws.send_json({"type":"stt_final","text":"Hello","target":start.get("target")})
    monkeypatch.setattr(stt_ws_routes, "run_stt_bridge", fake_bridge)
    monkeypatch.setattr(stt_ws_routes, "resolve_stt_provider", lambda p=None: "openai")
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    with TestClient(app).websocket_connect("/ws/stt") as ws:
        ws.send_json({"type":"stt_start","target":"prompt","provider":"openai"})
        assert ws.receive_json() == {"type":"stt_delta","text":"Hel","target":"prompt"}
        assert ws.receive_json() == {"type":"stt_final","text":"Hello","target":"prompt"}

def test_ws_stt_no_provider_errors(monkeypatch):
    import Orchestrator.app  # noqa: F401
    from Orchestrator.routes import stt_ws_routes
    monkeypatch.setattr(stt_ws_routes, "resolve_stt_provider", lambda p=None: None)
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    with TestClient(app).websocket_connect("/ws/stt") as ws:
        ws.send_json({"type":"stt_start","target":"prompt"})
        assert ws.receive_json()["type"] == "stt_error"

def test_ws_stt_first_message_must_be_start(monkeypatch):
    import Orchestrator.app  # noqa: F401
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    with TestClient(app).websocket_connect("/ws/stt") as ws:
        ws.send_json({"type":"stt_audio","pcm":"AAAA"})
        assert ws.receive_json()["type"] == "stt_error"
