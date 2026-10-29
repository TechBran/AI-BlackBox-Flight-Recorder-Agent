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

# tool -> (feature, provider, the env var its backend needs: one name, or a
# tuple of alternatives any one of which runs it)
GATED = {
    "lyria_music": ("music", "lyria", "GOOGLE_APPLICATION_CREDENTIALS"),
    "elevenlabs_music": ("music", "elevenlabs", "ELEVENLABS_API_KEY"),
    "elevenlabs_sound_effects": ("music", "elevenlabs", "ELEVENLABS_API_KEY"),
    "generate_video": ("video", "veo", "GOOGLE_API_KEY"),
    "extend_video": ("video", "veo", "GOOGLE_API_KEY"),
    # Cloud TTS on the service account first, else AI Studio on the API key.
    "gemini_pro_tts": ("tts", "gemini", ("GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")),
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


def _names(env):
    return env if isinstance(env, tuple) else (env,)


def test_each_key_opens_exactly_its_tools(monkeypatch):
    for env_var in sorted({n for v in GATED.values() for n in _names(v[2])}):
        _env(monkeypatch, {env_var: "test-key"})
        offered = set(tool_registry.get_group_tool_names("chat"))
        expected = {n for n, (_f, _p, e) in GATED.items() if env_var in _names(e)}
        assert offered & set(GATED) == expected, env_var


def test_gemini_tts_runs_on_the_service_account_alone(monkeypatch, tmp_path):
    """call_gemini_tts prefers Cloud TTS whenever GOOGLE_APPLICATION_CREDENTIALS
    is set, so a box with the service account and no API key keeps
    gemini_pro_tts (and Lyria) without a TTS_ENABLED override."""
    monkeypatch.setattr(availability, "_ROOT", str(tmp_path))
    monkeypatch.setattr(availability, "_local_image_available", lambda: False)
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY", "XAI_API_KEY",
                "TTS_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "sa.json"))
    offered = set(tool_registry.get_group_tool_names("chat"))
    assert {"gemini_pro_tts", "lyria_music"} <= offered
    assert not {"generate_video", "analyze_image", "elevenlabs_list_voices"} & offered
    assert "gemini" in availability.enabled_providers("tts")


def test_every_provider_env_var_is_read_from_the_process_env(monkeypatch, tmp_path):
    monkeypatch.setattr(availability, "_ROOT", str(tmp_path))
    for spec in availability.FEATURES.values():
        for value in spec["provider_env"].values():
            for name in availability.env_names(value):
                monkeypatch.setenv(name, "test-key")
                assert availability._read_env().get(name) == "test-key", name


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
