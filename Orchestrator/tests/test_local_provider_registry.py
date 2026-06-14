from Orchestrator.local_provider.registry import get_local_registry

def test_attest_then_status_roundtrip(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()  # fresh, file-backed
    reg.attest(operator="Brandon", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    status = reg.status(operator="Brandon")
    assert status["available"] is True
    assert status["models"][0]["model_slug"] == "gemma-4-e4b"
    assert status["models"][0]["autonomy_mode"] == "permission"

def test_status_unknown_operator_is_unavailable(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()
    assert reg.status(operator="Nobody")["available"] is False
