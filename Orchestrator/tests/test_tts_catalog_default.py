"""GET /tts/catalog publishes the box's default voice and per-group availability.

Measured defect (audit P02-voice-web-09 / O18-toolvault-modules-14, rank 39): the
catalog returned the three static groups whatever keys the box had and said
nothing about a default, so every client pinned its own -- the Portal
"gemini-pro:Charon", Android "openai:onyx", the text_to_speech tool
"onyx"/"tts-1-hd" -- and a fresh box with one provider spoke through a provider
it had no key for. Now each group carries `available` (the running process holds
the provider's credentials; a key saved but not loaded yet asks for a restart)
and `model` (what the box synthesises that group with), and the catalog
carries `default`: the [audio] voice when its provider is usable, else the first
usable group. All additive: existing readers ignore the new keys.

Hermetic: credentials, the ElevenLabs catalog, the local registry and the
on-box stack are all monkeypatched; no network.
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator import config
from Orchestrator.config import (
    GEMINI_TTS_MODELS, build_tts_catalog, resolve_tts_default,
)


def _creds(monkeypatch, openai=False, google=False):
    monkeypatch.setattr(config, "_tts_credentials",
                        lambda: {"openai": openai, "google": google})
    # Hermetic: never consult this box's real .env for the "restart" reason.
    monkeypatch.setattr(config, "_tts_saved_keys",
                        lambda: {"openai": False, "google": False})


@pytest.fixture
def onyx(monkeypatch):
    monkeypatch.setattr(config, "TTS_VOICE", "onyx")


# ── build_tts_catalog: availability + model on every static group ───────────
def test_static_groups_carry_availability_and_model(monkeypatch):
    _creds(monkeypatch, openai=True, google=False)
    g = {x["id"]: x for x in build_tts_catalog()}
    assert g["openai"]["available"] is True
    assert g["openai"]["model"] == config.TTS_MODEL
    assert "unavailable_reason" not in g["openai"]
    for gid in ("gemini-flash", "gemini-pro"):
        assert g[gid]["available"] is False
        assert "GOOGLE_API_KEY" in g[gid]["unavailable_reason"]
        assert g[gid]["model"] == GEMINI_TTS_MODELS[gid]
    assert GEMINI_TTS_MODELS["gemini-flash"] != GEMINI_TTS_MODELS["gemini-pro"]


def test_group_order_and_voices_unchanged_by_the_new_fields(monkeypatch):
    _creds(monkeypatch, openai=False, google=False)
    groups = build_tts_catalog()
    assert [x["id"] for x in groups] == ["openai", "gemini-flash", "gemini-pro"]
    assert all(v["id"].startswith(x["id"] + ":") for x in groups for v in x["voices"])


def _process_keys(monkeypatch, tmp_path, openai="", google="", service_account=False,
                  dotenv=""):
    """The keys the RUNNING process holds (frozen at import, what the synth
    paths use) and what .env on disk holds (what the wizard just saved)."""
    from Orchestrator.onboarding import secrets_writer
    monkeypatch.setattr(config, "OPENAI_API_KEY", openai)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", google)
    monkeypatch.setattr(config, "USE_CLOUD_TTS", service_account)
    monkeypatch.setattr(config, "GOOGLE_AUTH_AVAILABLE", service_account)
    env = tmp_path / ".env"
    env.write_text(dotenv)
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env)
    for k in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)


def test_availability_follows_the_keys_the_running_process_synthesises_with(monkeypatch, tmp_path):
    """`available` must match what /tts can speak NOW: tts_routes synthesises with
    the import-time keys, so a key only on disk does not count yet."""
    _process_keys(monkeypatch, tmp_path, openai="sk-test-running")
    assert config._tts_credentials() == {"openai": True, "google": False}
    _process_keys(monkeypatch, tmp_path, google="g-test-running")
    assert config._tts_credentials() == {"openai": False, "google": True}
    _process_keys(monkeypatch, tmp_path, service_account=True)
    assert config._tts_credentials() == {"openai": False, "google": True}
    _process_keys(monkeypatch, tmp_path, dotenv="OPENAI_API_KEY=sk-test-saved\n")
    assert config._tts_credentials() == {"openai": False, "google": False}


def test_a_key_saved_but_not_yet_loaded_asks_for_a_restart(monkeypatch, tmp_path):
    """Wizard saved an OpenAI key on a Google-only box: until the restart that
    /onboarding/restart-status asks for, OpenAI stays unavailable (the process
    cannot use it) and the default stays a Gemini voice the box can speak."""
    _process_keys(monkeypatch, tmp_path, google="g-test-running",
                  dotenv="OPENAI_API_KEY=sk-test-saved\n")
    monkeypatch.setattr(config, "TTS_VOICE", "onyx")
    groups = build_tts_catalog()
    openai = next(g for g in groups if g["id"] == "openai")
    assert openai["available"] is False
    assert "restart" in openai["unavailable_reason"].lower()
    gemini = next(g for g in groups if g["id"] == "gemini-pro")
    assert gemini["available"] is True and "unavailable_reason" not in gemini
    assert resolve_tts_default(groups)["provider"] == "gemini-pro"


def test_a_missing_key_names_the_key(monkeypatch, tmp_path):
    _process_keys(monkeypatch, tmp_path)
    groups = {g["id"]: g for g in build_tts_catalog()}
    assert groups["openai"]["unavailable_reason"] == "Needs OPENAI_API_KEY"
    assert "GOOGLE_API_KEY" in groups["gemini-flash"]["unavailable_reason"]


def test_a_configured_openai_voice_the_static_list_lacks_is_listed_and_is_the_default(monkeypatch):
    """[audio] voice = marin (gpt-4o-mini-tts): listed in the OpenAI group so the
    picker can show it, and kept as the default instead of a silent swap to alloy."""
    _creds(monkeypatch, openai=True, google=True)
    monkeypatch.setattr(config, "TTS_VOICE", "marin")
    groups = build_tts_catalog()
    openai = next(g for g in groups if g["id"] == "openai")
    assert [v["id"] for v in openai["voices"]].count("openai:marin") == 1
    d = resolve_tts_default(groups)
    assert (d["id"], d["source"]) == ("openai:marin", "config")


def test_a_listed_configured_voice_is_not_duplicated(monkeypatch, onyx):
    _creds(monkeypatch, openai=True)
    openai = next(g for g in build_tts_catalog() if g["id"] == "openai")
    assert [v["id"] for v in openai["voices"]].count("openai:onyx") == 1
    assert len(openai["voices"]) == len(config.OPENAI_TTS_VOICES)


# ── resolve_tts_default ──────────────────────────────────────────────────────
def test_default_is_the_configured_voice_when_its_provider_is_usable(monkeypatch, onyx):
    _creds(monkeypatch, openai=True, google=True)
    d = resolve_tts_default(build_tts_catalog())
    assert d == {"id": "openai:onyx", "provider": "openai", "voice": "onyx",
                 "model": config.TTS_MODEL, "available": True, "source": "config"}


def test_default_falls_back_when_the_configured_provider_has_no_key(monkeypatch, onyx):
    """The fresh Google-only box: [audio] voice says onyx, there is no OpenAI key."""
    _creds(monkeypatch, openai=False, google=True)
    d = resolve_tts_default(build_tts_catalog())
    assert d["provider"] == "gemini-pro"
    assert d["id"] == f"gemini-pro:{config.GEMINI_TTS_DEFAULT_VOICE}"
    assert d["model"] == GEMINI_TTS_MODELS["gemini-pro"]
    assert d["available"] is True and d["source"] == "fallback"


def test_default_reaches_dynamic_groups(monkeypatch, onyx):
    """An ElevenLabs-only box speaks with an ElevenLabs voice, not OpenAI."""
    _creds(monkeypatch, openai=False, google=False)
    groups = build_tts_catalog() + [{
        "id": "elevenlabs", "label": "ElevenLabs", "dynamic": True,
        "available": True, "model": "eleven_v3",
        "voices": [{"id": "elevenlabs:own1", "name": "Mine", "description": "cloned"}]}]
    d = resolve_tts_default(groups)
    assert d["id"] == "elevenlabs:own1" and d["provider"] == "elevenlabs"
    assert d["voice"] == "own1" and d["source"] == "fallback"


def test_prefixed_configured_voice_is_honoured(monkeypatch):
    monkeypatch.setattr(config, "TTS_VOICE", "elevenlabs:own1")
    _creds(monkeypatch, openai=True, google=True)
    groups = build_tts_catalog() + [{
        "id": "elevenlabs", "label": "ElevenLabs", "available": True, "model": "eleven_v3",
        "voices": [{"id": "elevenlabs:other", "name": "Other", "description": ""},
                   {"id": "elevenlabs:own1", "name": "Mine", "description": ""}]}]
    d = resolve_tts_default(groups)
    assert d["id"] == "elevenlabs:own1" and d["source"] == "config"


def test_no_usable_provider_reports_the_configured_voice_unavailable(monkeypatch, onyx):
    _creds(monkeypatch, openai=False, google=False)
    d = resolve_tts_default(build_tts_catalog())
    assert d["id"] == "openai:onyx"
    assert d["available"] is False and d["source"] == "none"


# ── GET /tts/catalog ─────────────────────────────────────────────────────────
@pytest.fixture
def cli(monkeypatch):
    from Orchestrator.app import app
    from Orchestrator.elevenlabs import catalog as el_catalog
    from Orchestrator import qwen_tts
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: None)
    monkeypatch.setattr("Orchestrator.onboarding.custom_servers.has_audio",
                        lambda kind: False)
    monkeypatch.setattr(qwen_tts, "catalog_group", lambda: None)
    return TestClient(app)


def test_catalog_route_emits_default_and_availability(cli, monkeypatch, onyx):
    _creds(monkeypatch, openai=True, google=False)
    body = cli.get("/tts/catalog").json()
    assert [g["id"] for g in body["groups"]] == ["openai", "gemini-flash", "gemini-pro"]
    assert {g["id"]: g["available"] for g in body["groups"]} == {
        "openai": True, "gemini-flash": False, "gemini-pro": False}
    assert body["default"]["id"] == "openai:onyx"
    assert body["default"]["available"] is True


def test_catalog_route_default_and_model_for_an_elevenlabs_only_box(cli, monkeypatch, onyx):
    from Orchestrator.elevenlabs import catalog as el_catalog
    _creds(monkeypatch, openai=False, google=False)
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: {
        "my_voices": [{"id": "elevenlabs:own1", "name": "Mine", "description": "cloned"}],
        "premade": [{"id": "elevenlabs:pre1", "name": "Rachel", "description": "female"}],
    })
    body = cli.get("/tts/catalog").json()
    el = body["groups"][-1]
    assert el["id"] == "elevenlabs"
    assert el["available"] is True
    assert el["model"] == config.ELEVENLABS_TTS_MODEL_DEFAULT
    assert body["default"]["id"] == "elevenlabs:own1"
    assert body["default"]["provider"] == "elevenlabs"
