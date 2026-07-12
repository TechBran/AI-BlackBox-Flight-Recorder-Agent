# Orchestrator/tests/test_voice_agents_registry.py
"""Voice-agent preset registry: round-trip, corruption quarantine, atomicity, 0600."""
import json, os, stat
import pytest
from Orchestrator.voice_agents import registry as va


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "voice_agents.json"
    monkeypatch.setattr(va, "REGISTRY_PATH", str(path))
    return path


def test_list_presets_absent_file_returns_empty(reg):
    assert va.list_presets() == []


def test_corrupt_file_quarantined_and_empty(reg):
    reg.write_text("{not json")
    assert va.list_presets() == []            # fail-soft, never raises
    quarantined = [p for p in reg.parent.iterdir() if ".corrupt-" in p.name]
    assert len(quarantined) == 1              # original preserved for forensics


def test_wrong_shape_quarantined(reg):
    reg.write_text(json.dumps({"version": 1, "agents": "nope"}))
    assert va.list_presets() == []
    assert any(".corrupt-" in p.name for p in reg.parent.iterdir())


def test_add_preset_persists_round_trip(reg):
    p = va.add_preset(name="Pizza Bot", provider="grok-live", created_by="Brandon",
                      voice="Rex", instructions="You order pizzas.", greeting="Hi!")
    assert p["id"].startswith("va-")
    assert p["created_at"] and p["updated_at"]
    on_disk = json.loads(reg.read_text())
    assert on_disk["version"] == 1
    assert on_disk["agents"][0]["name"] == "Pizza Bot"
    assert va.get_preset(p["id"])["voice"] == "Rex"


def test_registry_file_is_0600(reg):
    va.add_preset(name="a", provider="realtime")
    assert stat.S_IMODE(os.stat(reg).st_mode) == 0o600


def test_atomic_write_no_tmp_left_behind(reg):
    va.add_preset(name="a", provider="realtime")
    leftovers = [p for p in reg.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_provider_must_be_known(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="x", provider="elevenlabs")


def test_name_unique_case_insensitive(reg):
    va.add_preset(name="Bot", provider="realtime")
    with pytest.raises(ValueError):
        va.add_preset(name="bot", provider="gemini-live")


def test_instructions_size_cap(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="big", provider="realtime",
                      instructions="x" * (va.INSTRUCTIONS_MAX_CHARS + 1))


def test_keyterms_validated(reg):
    with pytest.raises(ValueError):
        va.add_preset(name="k", provider="grok-live", keyterms=["ok", 42])
    with pytest.raises(ValueError):
        va.add_preset(name="k2", provider="grok-live",
                      keyterms=[f"t{i}" for i in range(va.KEYTERMS_MAX + 1)])


def test_update_bumps_updated_at_and_delete(reg):
    p = va.add_preset(name="a", provider="realtime")
    va.update_preset(p["id"], {"voice": "marin"})
    got = va.get_preset(p["id"])
    assert got["voice"] == "marin"
    assert got["updated_at"] >= got["created_at"]
    with pytest.raises(ValueError):
        va.update_preset(p["id"], {"id": "va-hax"})   # unpatchable field
    with pytest.raises(KeyError):
        va.update_preset("va-nope", {"voice": "x"})
    va.delete_preset(p["id"])
    assert va.list_presets() == []
    with pytest.raises(KeyError):
        va.delete_preset(p["id"])
