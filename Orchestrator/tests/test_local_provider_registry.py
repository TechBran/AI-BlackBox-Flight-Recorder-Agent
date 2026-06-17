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

def test_set_autonomy_flips_and_persists(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()
    reg.attest(operator="Brandon", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    updated = reg.set_autonomy("Brandon", "pixel-9", "yolo")
    assert updated["autonomy_mode"] == "yolo"
    # Fresh instance reads from the same patched store -> proves persistence.
    reloaded = r.LocalProviderRegistry()
    assert reloaded.status(operator="Brandon")["models"][0]["autonomy_mode"] == "yolo"

def test_set_autonomy_unknown_returns_none(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()
    assert reg.set_autonomy("Nobody", "x", "yolo") is None

def test_remove_prunes_empty_operator(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()
    reg.attest(operator="Brandon", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    assert reg.remove("Brandon", "pixel-9") is True
    assert reg.status(operator="Brandon")["available"] is False
    # Operator key fully pruned from the internal store once its last device is gone.
    assert "Brandon" not in reg._store
    # Idempotent: removing the now-missing record returns False.
    assert reg.remove("Brandon", "pixel-9") is False
