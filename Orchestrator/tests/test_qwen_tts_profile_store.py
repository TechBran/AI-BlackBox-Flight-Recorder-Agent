"""Unit tests for the qwen-tts profile store — pure stdlib, no FastAPI, no model.

Isolation: QWEN_TTS_VOICES_DIR points at a tmp dir per test (never the real
Manifest/voices/qwen). Same isolation recipe as the embeddings-route tests.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import json

import pytest

from qwen_tts_server import profile_store


@pytest.fixture
def voices(tmp_path, monkeypatch):
    d = tmp_path / "qwen"
    monkeypatch.setenv("QWEN_TTS_VOICES_DIR", str(d))
    return d


def test_sanitize_slug_basic():
    assert profile_store.sanitize_slug("My Cool Voice") == "my-cool-voice"


def test_sanitize_slug_strips_traversal():
    slug = profile_store.sanitize_slug("../../etc/passwd")
    assert "/" not in slug and ".." not in slug
    assert slug == "etc-passwd"


def test_sanitize_slug_empty_raises():
    with pytest.raises(ValueError):
        profile_store.sanitize_slug("///")


def test_unique_slug_suffixes_on_collision(voices):
    (voices / "brandon").mkdir(parents=True)
    assert profile_store.unique_slug("Brandon") == "brandon-2"


def test_save_clone_profile_persists(voices):
    prof = profile_store.save_clone_profile(
        "brandon", "Brandon", "system", True, b"RIFFfake", "ref.wav", sample_rate=22050
    )
    assert prof["variant"] == "base"
    on_disk = json.loads((voices / "brandon" / "profile.json").read_text())
    assert on_disk["consent"] is True
    assert on_disk["sample_rate"] == 22050
    assert (voices / "brandon" / "reference.wav").read_bytes() == b"RIFFfake"


def test_atomic_write_leaves_no_tmp(voices):
    profile_store.save_design_profile("d1", "D1", "system", "warm", {"seed": 1})
    assert list((voices / "d1").glob(".*tmp")) == []


def test_list_and_get_profiles(voices):
    profile_store.save_design_profile("d1", "D1", "system", "warm", {"seed": 1})
    assert [p["slug"] for p in profile_store.list_profiles()] == ["d1"]
    assert profile_store.get_profile("d1")["name"] == "D1"
    assert profile_store.get_profile("nope") is None


def test_ref_audio_path_and_delete(voices):
    profile_store.save_clone_profile("b", "B", "system", True, b"aud", "r.wav")
    assert profile_store.ref_audio_path("b").endswith("/b/reference.wav")
    assert profile_store.delete_profile("b") is True
    assert profile_store.get_profile("b") is None
