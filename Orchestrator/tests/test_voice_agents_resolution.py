# Orchestrator/tests/test_voice_agents_resolution.py
"""resolve_preset / merge_connect_params / resolve_phone_role.

Precedence contract (design doc W3): explicit params > preset fields > defaults.
"""
import pytest
from Orchestrator.voice_agents import registry as va


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))
    return tmp_path


def test_resolve_preset_by_id(reg):
    p = va.add_preset(name="Bot", provider="realtime", voice="marin")
    assert va.resolve_preset(p["id"])["voice"] == "marin"


def test_resolve_preset_unknown_returns_none(reg):
    assert va.resolve_preset("va-nope") is None
    assert va.resolve_preset("") is None
    assert va.resolve_preset(None) is None


def test_resolve_preset_provider_mismatch_returns_none(reg):
    p = va.add_preset(name="Bot", provider="grok-live")
    assert va.resolve_preset(p["id"], provider="realtime") is None
    assert va.resolve_preset(p["id"], provider="grok-live") is not None


def test_merge_explicit_wins_over_preset():
    preset = {"model": "p-model", "voice": "p-voice", "instructions": "p-inst"}
    merged = va.merge_connect_params(
        {"model": "x-model", "voice": "", "instructions": None}, preset)
    assert merged["model"] == "x-model"        # explicit wins
    assert merged["voice"] == "p-voice"        # "" falls through to preset
    assert merged["instructions"] == "p-inst"  # None falls through to preset


def test_merge_no_preset_yields_explicit_or_none():
    merged = va.merge_connect_params({"voice": "ash"}, None)
    assert merged["voice"] == "ash"
    assert merged["model"] is None             # defaults stay with the route


def test_merge_empty_preset_values_yield_none():
    merged = va.merge_connect_params({}, {"model": "", "keyterms": []})
    assert merged["model"] is None and merged["keyterms"] is None


def test_resolve_phone_role_passthrough_when_not_preset(reg):
    assert va.resolve_phone_role("Be a pirate", "openai_realtime", "hi") == \
        ("Be a pirate", "openai_realtime", "hi")


def test_resolve_phone_role_substitutes_preset(reg):
    p = va.add_preset(name="Pizza", provider="grok-live",
                      instructions="You order pizzas.", greeting="Hello!")
    role, backend, greeting = va.resolve_phone_role(f"preset:{p['id']}", "openai_realtime", "")
    assert role == "You order pizzas."
    assert backend == "grok_live"              # preset provider drives the backend
    assert greeting == "Hello!"                # preset fills empty greeting
    # explicit greeting wins over the preset's
    assert va.resolve_phone_role(f"preset:{p['id']}", "x", "custom")[2] == "custom"


def test_resolve_phone_role_unknown_id_raises(reg):
    with pytest.raises(KeyError):
        va.resolve_phone_role("preset:va-nope", "openai_realtime", "")
