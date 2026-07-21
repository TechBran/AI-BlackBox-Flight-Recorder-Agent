"""Orchestrator-side Qwen3-TTS integration helpers (M7 Task 7.1).

Pure-helper contracts (no FastAPI): the catalog group is fail-open on the
on-box TTS availability seam (_tts_available), the preset list matches the
spec §5.4/§14 verified 9 CustomVoice voices, saved profiles are read from
Manifest/voices/qwen/ (never wakes the GPU), synthesize() POSTs the
llama-swap /v1/audio/speech proxy with model="qwen-tts", and upstream_url()
strips the /v1 suffix to build the /upstream/qwen-tts passthrough.
"""
from unittest.mock import patch

from Orchestrator import qwen_tts


def test_preset_voices_are_the_nine_customvoice_presets():
    names = [n for n, _desc in qwen_tts.QWEN_PRESET_VOICES]
    assert names == [
        "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
        "Ryan", "Aiden", "Ono_Anna", "Sohee",
    ]


def test_catalog_group_absent_when_tts_unavailable():
    with patch("Orchestrator.qwen_tts._tts_available", return_value=False):
        assert qwen_tts.catalog_group() is None


def test_catalog_group_presets_only_when_no_profiles():
    with patch("Orchestrator.qwen_tts._tts_available", return_value=True), \
         patch("Orchestrator.qwen_tts.list_profiles", return_value=[]):
        g = qwen_tts.catalog_group()
    assert g["id"] == "qwen"
    assert g["label"] == "Qwen3-TTS (On-Box)"
    assert g["dynamic"] is True
    assert len(g["voices"]) == 9
    assert g["voices"][0]["id"] == "qwen:Vivian"
    # underscores humanized for display only; the id keeps the raw voice token
    fu = next(v for v in g["voices"] if v["id"] == "qwen:Uncle_Fu")
    assert fu["name"] == "Uncle Fu"


def test_catalog_group_appends_saved_profiles_star_prefixed():
    prof = [{"slug": "brandon-clone", "name": "Brandon", "variant": "base"}]
    with patch("Orchestrator.qwen_tts._tts_available", return_value=True), \
         patch("Orchestrator.qwen_tts.list_profiles", return_value=prof):
        g = qwen_tts.catalog_group()
    last = g["voices"][-1]
    assert last["id"] == "qwen:brandon-clone"
    assert last["name"] == "⭐ Brandon"        # star-prefixed like ElevenLabs My Voices


def test_synthesize_posts_speech_proxy_with_member_model():
    captured = {}

    class _Resp:
        status_code = 200
        content = b"WAVDATA"
        text = ""

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    with patch("Orchestrator.qwen_tts._base_url", return_value="http://127.0.0.1:9098/v1"), \
         patch("Orchestrator.qwen_tts.requests.post", side_effect=_fake_post):
        r = qwen_tts.synthesize("Vivian", "hello", response_format="mp3")
    assert r.content == b"WAVDATA"
    assert captured["url"] == "http://127.0.0.1:9098/v1/audio/speech"
    assert captured["json"]["model"] == "qwen-tts"
    assert captured["json"]["voice"] == "Vivian"
    assert captured["json"]["input"] == "hello"
    assert captured["json"]["response_format"] == "mp3"


def test_upstream_url_strips_v1_and_targets_member():
    with patch("Orchestrator.qwen_tts._base_url", return_value="http://127.0.0.1:9098/v1"):
        assert qwen_tts.upstream_url("/v1/voices/clone") == \
            "http://127.0.0.1:9098/upstream/qwen-tts/v1/voices/clone"
