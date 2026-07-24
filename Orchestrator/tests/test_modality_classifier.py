"""Unified model-modality classifier + resolver + persisted map (custom registry)."""
import pytest
from Orchestrator.onboarding import custom_servers as cs


def test_classify_model_families():
    cases = {
        "z-image": "image", "flux.2-klein-4b": "image", "qwen-image": "image",
        "whisper-large-v3": "stt", "faster-whisper-medium": "stt", "parakeet-tdt": "stt",
        "kokoro-tts": "tts", "tts-1-hd": "tts", "xtts-v2": "tts", "piper-en": "tts",
        "bge-m3": "embedding", "nomic-embed-text": "embedding",
        "gemma-31b": "chat", "llama-3.3-70b": "chat", "mistral-small": "chat", "qwen3-8b": "chat",
    }
    for mid, expected in cases.items():
        assert cs.classify_model(mid) == expected, mid


def test_classify_model_rerank_and_audio_gateways_never_seed_chat():
    """MS02 llama-swap regression (2026-07-23): rerank-qwen3-8b and speaches
    seeded 'chat', leaked into /models/custom, and the reranker became
    default_id. Rerankers seed 'ignore' (checked FIRST so bge-reranker-* beats
    the 'bge' embedding pattern); the Speaches audio gateway seeds 'stt'."""
    assert cs.classify_model("rerank-qwen3-8b") == "ignore"
    assert cs.classify_model("bge-reranker-v2-m3") == "ignore"
    assert cs.classify_model("speaches") == "stt"
    # The full MS02 quartet, end to end:
    assert cs.classify_models(
        ["embed-qwen3-8b", "qwen-tts", "rerank-qwen3-8b", "speaches"]
    ) == {
        "embed-qwen3-8b": "embedding",
        "qwen-tts": "tts",
        "rerank-qwen3-8b": "ignore",
        "speaches": "stt",
    }


def test_classify_model_non_string():
    assert cs.classify_model(None) == "chat"
    assert cs.classify_model(123) == "chat"


def test_classify_models_map():
    assert cs.classify_models(["gemma-31b", "z-image", 5]) == {"gemma-31b": "chat", "z-image": "image"}


def test_is_image_model_backcompat():
    assert cs.is_image_model("z-image") is True
    assert cs.is_image_model("gemma-31b") is False
    assert cs.is_image_model(None) is False


def test_model_modality_persisted_wins():
    srv = {"model_modalities": {"weird-name": "image"}}
    assert cs.model_modality(srv, "weird-name") == "image"   # persisted overrides name
    assert cs.model_modality(srv, "z-image") == "image"      # fallback to classify
    assert cs.model_modality({}, "gemma-31b") == "chat"      # no map -> classify


# --- resolver -------------------------------------------------------------
def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_resolve_modality_server_picks_first(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "whisper-1", "kokoro-tts"]},
    ])
    assert cs.resolve_modality_server("stt")[1] == "whisper-1"
    assert cs.resolve_modality_server("tts")[1] == "kokoro-tts"
    assert cs.resolve_modality_server("image") is None


def test_model_modality_map_reroutes_resolution(monkeypatch):
    # a persisted (wizard-confirmed) map forces a bland name to a modality
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["my-voice"], "model_modalities": {"my-voice": "tts"}},
    ])
    assert cs.resolve_modality_server("tts")[1] == "my-voice"
    assert cs.has_modality_model("tts") is True
    assert cs.has_modality_model("image") is False


def test_resolve_image_server_still_works(monkeypatch):
    # back-compat wrapper -> resolve_modality_server("image")
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "z-image"]},
    ])
    assert cs.resolve_image_server()[1] == "z-image"


# --- persistence (schema) -------------------------------------------------
def test_model_modalities_patchable():
    assert "model_modalities" in cs._PATCHABLE_FIELDS


def test_validate_field_types_model_modalities():
    cs._validate_field_types({"model_modalities": {"z-image": "image"}})  # ok
    with pytest.raises(ValueError):
        cs._validate_field_types({"model_modalities": {"z-image": 5}})
    with pytest.raises(ValueError):
        cs._validate_field_types({"model_modalities": ["nope"]})


def test_add_server_stores_model_modalities(monkeypatch):
    captured = {}
    monkeypatch.setattr(cs, "_read", lambda: {"version": 1, "servers": []})
    monkeypatch.setattr(cs, "_write", lambda data: captured.update(data=data))
    srv = cs.add_server("box", "http://h/v1", "k", model_modalities={"z-image": "image"})
    assert srv["model_modalities"] == {"z-image": "image"}
