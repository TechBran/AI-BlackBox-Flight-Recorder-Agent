"""Provider media tools carry an x-availability gate (audit
O18-toolvault-modules-13).

Only the web-search and image tools were gated, so a fresh box with no
ELEVENLABS_API_KEY / GOOGLE_API_KEY / XAI_API_KEY still injected or discovered
the ElevenLabs, Gemini TTS, Lyria, Veo, analysis and xAI voice tools; the model
called them and they failed at run time. The music, video, tts and analysis
features gate them on the key their backend route actually needs.

Runs against the real ToolVault/tools modules with a faked environment (no
.env, no network).
"""
import pytest

from Orchestrator.toolvault import availability, registry
from Orchestrator.tools import tool_registry

# tool -> (feature, provider, the env var its backend needs)
GATED = {
    "lyria_music": ("music", "lyria", "GOOGLE_APPLICATION_CREDENTIALS"),
    "elevenlabs_music": ("music", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_sound_effects": ("music", "elevenlabs", "ELEVENLABS_API_KEY"),
    "generate_video": ("video", "veo", "GOOGLE_API_KEY"),
    "extend_video": ("video", "veo", "GOOGLE_API_KEY"),
    "gemini_pro_tts": ("tts", "gemini", "GOOGLE_API_KEY"),
    "elevenlabs_clone_voice": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_delete_voice": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_design_voice": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_isolate_voice": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_list_voices": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_voice_changer": ("tts", "elevenlabs", "ELEVENLABS_API_KEY"),
    "xai_clone_voice": ("tts", "xai", "XAI_API_KEY"),
    "analyze_image": ("analysis", "gemini", "GOOGLE_API_KEY"),
    "analyze_audio": ("analysis", "gemini", "GOOGLE_API_KEY"),
    "analyze_video": ("analysis", "gemini", "GOOGLE_API_KEY"),
}

# Catalog-driven tools that also reach on-box engines: never key-gated.
UNGATED = ("text_to_speech", "list_tts_voices", "speech_to_text")


@pytest.fixture(autouse=True)
def _fresh_registry():
    registry.invalidate_cache()
    tool_registry.reset_cache()
    yield
    registry.invalidate_cache()
    tool_registry.reset_cache()


def _env(monkeypatch, env):
    monkeypatch.setattr(availability, "_read_env", lambda: dict(env))
    monkeypatch.setattr(availability, "_local_image_available", lambda: False)


@pytest.mark.parametrize("name", sorted(GATED))
def test_tool_declares_the_gate_for_its_provider(name):
    feature, provider, env = GATED[name]
    gate = registry.get_tool(name)["x-availability"]
    assert gate["feature"] == feature
    assert gate["provider"] == provider
    assert availability.FEATURES[feature]["provider_env"][provider] == env


@pytest.mark.parametrize("name", UNGATED)
def test_catalog_driven_voice_tools_stay_ungated(name):
    assert "x-availability" not in registry.get_tool(name)


def test_a_fresh_box_without_keys_offers_none_of_them(monkeypatch):
    _env(monkeypatch, {})
    for group in ("chat", "mcp", "gemini_live", "realtime", "grok_live", "phone"):
        offered = set(tool_registry.get_group_tool_names(group))
        assert not offered & set(GATED), (group, sorted(offered & set(GATED)))
    assert set(UNGATED) <= set(tool_registry.get_group_tool_names("chat"))


def test_each_key_opens_exactly_its_tools(monkeypatch):
    for env_var in sorted({v[2] for v in GATED.values()}):
        _env(monkeypatch, {env_var: "test-key"})
        offered = set(tool_registry.get_group_tool_names("chat"))
        expected = {n for n, (_f, _p, e) in GATED.items() if e == env_var}
        assert offered & set(GATED) == expected, env_var


def test_the_gemini_key_alias_opens_the_google_keyed_tools(monkeypatch, tmp_path):
    # The real env reader: no .env under the root, GEMINI_API_KEY alone.
    monkeypatch.setattr(availability, "_ROOT", str(tmp_path))
    monkeypatch.setattr(availability, "_local_image_available", lambda: False)
    for var in ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    offered = set(tool_registry.get_group_tool_names("chat"))
    assert {"generate_video", "gemini_pro_tts", "analyze_image"} <= offered
    assert "lyria_music" not in offered          # needs the service account


def test_an_explicit_enabled_pref_narrows_a_feature(monkeypatch):
    _env(monkeypatch, {"ELEVENLABS_API_KEY": "k", "XAI_API_KEY": "k",
                       "TTS_ENABLED": "xai"})
    offered = set(tool_registry.get_group_tool_names("chat"))
    assert "xai_clone_voice" in offered
    assert "elevenlabs_list_voices" not in offered
    # The music feature is unaffected by the tts pref.
    assert "elevenlabs_music" in offered


# Backend routes / modules that refuse to work without a provider credential.
_KEYED_BACKENDS = ("/generate/video", "/extend/video", "/generate/lyria_music",
                   "/generate/gemini_tts", "/generate/elevenlabs", "/elevenlabs/",
                   "/analyze/audio", "Orchestrator.media_analysis",
                   "Orchestrator.elevenlabs", "xai_voices")


def test_every_executor_that_needs_a_provider_key_is_gated():
    """The audit's guard: a tool whose executor reaches a keyed backend must
    declare a gate, so the next provider tool cannot ship ungated."""
    ungated = []
    for executor in sorted(registry.TOOLS_DIR.glob("*/executor.py")):
        source = executor.read_text(encoding="utf-8")
        if any(marker in source for marker in _KEYED_BACKENDS):
            tool = registry.get_tool(executor.parent.name)
            if tool is not None and "x-availability" not in tool:
                ungated.append(executor.parent.name)
    assert ungated == [], f"provider-keyed tools without an x-availability gate: {ungated}"
    assert set(GATED) <= {e.parent.name for e in registry.TOOLS_DIR.glob("*/executor.py")}
