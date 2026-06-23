"""Backend /onboarding/status rollup + SSE live re-validation (M1).

The rollup canonicalizes the provider->step join + attention-derivation rules
server-side so the hub frontend stays presentational. SECTIONS is the 10-section
catalog (welcome/done are the hub itself, NOT sections); it must never drift from
state.ALL_STEPS. Hermeticity mirrors test_onboarding_web_search.py's tmp_env.
"""
from Orchestrator.onboarding import status_rollup as sr
from Orchestrator.onboarding.state import ALL_STEPS


# Steps that are the hub itself, not status sections (design: welcome->hub
# basis, done->summary model).
_NON_SECTION_STEPS = {"welcome", "done"}


def test_sections_keys_are_all_steps_minus_welcome_and_done():
    section_keys = [s["key"] for s in sr.SECTIONS]
    expected = [s for s in ALL_STEPS if s not in _NON_SECTION_STEPS]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_every_section_step_equals_its_key():
    # Contract: step == key for every section (the hub links ?step=<key>).
    for s in sr.SECTIONS:
        assert s["step"] == s["key"], f"{s['key']}: step != key"


def test_sections_have_required_shape():
    valid_groups = {"network", "keys", "capabilities", "identity"}
    for s in sr.SECTIONS:
        assert set(s) >= {"key", "group", "label", "required", "step"}
        assert s["group"] in valid_groups
        assert isinstance(s["required"], bool)


def _empty_inputs():
    """Minimal snapshot inputs for build_status (all unconfigured)."""
    return dict(
        env={},
        state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
        embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
        cli={"providers": {}, "ready": False},
        web_search={"enabled": [], "providers": {}, "default": ""},
        image={"enabled": [], "providers": {}, "default": ""},
        paired=[],
        operators=[],
        restart={"needs_restart": False, "drifted_keys": []},
    )


def _section(rollup, key):
    return next(s for s in rollup["sections"] if s["key"] == key)


def test_api_keys_attention_when_no_keys_present():
    rollup = sr.build_status(**_empty_inputs())
    sec = _section(rollup, "api_keys")
    # required + unsatisfied -> attention (NOT optional)
    assert sec["state"] == sr.ATTENTION
    assert sec["required"] is True


def test_api_keys_ready_when_a_key_present_and_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "api_keys")["state"] == sr.READY


def test_api_keys_attention_when_present_but_never_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}  # present, validated_at empty
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert any(a["section"] == "api_keys" and a["severity"] == "warn"
               for a in rollup["attention"])


def test_operator_required_attention_when_none():
    rollup = sr.build_status(**_empty_inputs())
    assert _section(rollup, "operator")["state"] == sr.ATTENTION


def test_operator_ready_when_present():
    inp = _empty_inputs()
    inp["operators"] = ["Brandon"]
    rollup = sr.build_status(**inp)
    assert _section(rollup, "operator")["state"] == sr.READY


def test_rollup_top_level_shape():
    rollup = sr.build_status(**_empty_inputs())
    assert set(rollup) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert rollup["total"] == len(sr.SECTIONS)
    assert isinstance(rollup["ready_count"], int)
    for sec in rollup["sections"]:
        assert set(sec) >= {"key", "group", "label", "state", "required",
                            "summary", "step", "skipped", "items"}


def test_embeddings_attention_when_no_active_model():
    inp = _empty_inputs()
    inp["embeddings"] = {"active": None, "health": {"state": "ok"},
                         "stores": [], "models": []}
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.ATTENTION


def test_embeddings_ready_when_active_and_healthy_and_caught_up():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "ok", "successor": None},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}],
        "models": [],
    }
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.READY


def test_embeddings_attention_when_index_behind():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "ok"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 42}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION
    assert any("behind" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "embeddings")


def test_embeddings_attention_when_health_superseded():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "superseded", "successor": "gemini-embedding-2"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION


def test_embeddings_attention_when_health_broken_is_error_severity():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "broken", "detail": "x"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert any(a["section"] == "embeddings" and a["severity"] == "error"
               for a in rollup["attention"])


def test_web_search_optional_when_nothing_enabled():
    inp = _empty_inputs()
    inp["web_search"] = {"enabled": [], "providers": {}, "default": ""}
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.OPTIONAL


def test_web_search_ready_when_enabled_with_keys():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["duckduckgo", "openai"],
        "providers": {"duckduckgo": {"key_present": True, "enabled": True},
                      "openai": {"key_present": True, "enabled": True}},
        "default": "openai",
    }
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.READY


def test_web_search_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["openai"],
        "providers": {"openai": {"key_present": False, "enabled": True}},
        "default": "openai",
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "web_search")["state"] == sr.ATTENTION
    assert any(a["section"] == "web_search" and "key" in a["message"].lower()
               for a in rollup["attention"])


def test_image_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["image"] = {
        "enabled": ["gemini"],
        "providers": {"gemini": {"key_present": False, "enabled": True}},
        "default": "gemini",
    }
    assert _section(sr.build_status(**inp), "image")["state"] == sr.ATTENTION


def test_cli_agents_attention_when_installed_not_authed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": False}}, "ready": False}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "cli_agents")["state"] == sr.ATTENTION
    assert any("auth" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "cli_agents")


def test_cli_agents_ready_when_all_ready():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": True}}, "ready": True}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.READY


def test_cli_agents_optional_when_none_installed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": False, "authenticated": False}}, "ready": False}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.OPTIONAL


def test_pair_phone_ready_when_devices_paired():
    inp = _empty_inputs()
    inp["paired"] = [{"name": "Pixel"}]
    assert _section(sr.build_status(**inp), "pair_phone")["state"] == sr.READY


def test_pair_phone_optional_when_none():
    assert _section(sr.build_status(**_empty_inputs()), "pair_phone")["state"] == sr.OPTIONAL


def test_global_restart_drift_emits_attention_row():
    inp = _empty_inputs()
    inp["restart"] = {"needs_restart": True, "drifted_keys": ["OPENAI_API_KEY"]}
    rollup = sr.build_status(**inp)
    assert any(a.get("section") is None and "restart" in a["message"].lower()
               for a in rollup["attention"])
