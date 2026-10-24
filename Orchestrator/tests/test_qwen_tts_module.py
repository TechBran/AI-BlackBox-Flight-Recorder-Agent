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


# ── honest availability + the tts breaker (2026-08-11 plan Tasks 9-10) ───────
import pytest
import requests

from Orchestrator import local_stack


class _R:
    """A requests.Response stand-in: status, text and an optional JSON body."""
    def __init__(self, status, body=None, text=None, content=b"WAV"):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (str(body) if body else "")
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


NO_GPU = _R(503, {"detail": "No CUDA GPU: the NVIDIA driver is not loaded (check nvidia-smi).",
                  "code": "no_gpu"})
VRAM = _R(503, {"detail": "insufficient free VRAM after unload", "code": "vram"})


@pytest.fixture
def stack_up(monkeypatch):
    """On-box TTS seeded, front door up, GPU fine — the only variable left is
    the capability signal under test."""
    monkeypatch.setattr(local_stack, "is_healthy", lambda *a, **k: True)
    monkeypatch.setattr(local_stack, "enabled", lambda cap: cap == "tts")
    monkeypatch.setattr(local_stack, "gpu_ready", lambda: {"ok": True, "reason": None})
    monkeypatch.setattr(qwen_tts, "_base_url", lambda: "http://127.0.0.1:9098/v1")


def _post_returning(*responses):
    seq = list(responses)
    posts = []

    def post(url, json=None, timeout=None):
        posts.append(url)
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r
    return post, posts


def test_tts_available_when_stack_up(stack_up):
    assert qwen_tts._tts_available() is True


def test_tts_unavailable_when_the_gpu_driver_is_dead(stack_up, monkeypatch):
    """The catalog drops the qwen group (and /tts/queue 503s) instead of
    advertising voices that 500 on every synthesis (plan Task 10, D4)."""
    monkeypatch.setattr(local_stack, "gpu_ready", lambda: {
        "ok": False, "reason": "NVIDIA driver not responding"})
    assert qwen_tts._tts_available() is False
    assert qwen_tts.catalog_group() is None


def test_tts_unavailable_while_the_breaker_is_open(stack_up):
    local_stack.report_failure("tts", "HTTP 500", fatal=True)
    assert qwen_tts._tts_available() is False


def test_tts_available_survives_a_local_stack_without_the_capability_signal(stack_up, monkeypatch):
    """A half-pulled deploy must not silently kill on-box TTS (the gate's bare
    except turns an AttributeError into False)."""
    monkeypatch.delattr(local_stack, "capability_problem")
    assert qwen_tts._tts_available() is True


def test_synthesize_reports_outcomes_to_the_breaker(stack_up):
    post, _ = _post_returning(_R(500, text="Internal Server Error"))
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        for _ in range(local_stack.BREAKER_THRESHOLD):
            assert qwen_tts.synthesize("Vivian", "hi").status_code == 500
    st = local_stack.breaker_state("tts")
    assert st["state"] == "open" and "HTTP 500" in st["last_error"]


def test_synthesize_refuses_fast_while_the_breaker_is_open(stack_up):
    """No request reaches a member known to be broken: the caller gets the
    reason at once (and /tts answers its fallback envelope with it)."""
    local_stack.report_failure("tts", "HTTP 503: no GPU", fatal=True)
    post, posts = _post_returning(_R(200))
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        with pytest.raises(qwen_tts.QwenCapabilityUnavailable) as exc:
            qwen_tts.synthesize("Vivian", "hi")
    assert posts == []
    assert "no GPU" in str(exc.value)


def test_contention_and_bad_requests_do_not_trip_the_breaker(stack_up, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_429_DEADLINE_S", "0")
    for resp in (VRAM, _R(429, text="too many"), _R(400, text="bad format"),
                 _R(404, text="unknown voice")):
        post, _ = _post_returning(resp)
        with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
            for _ in range(local_stack.BREAKER_THRESHOLD + 1):
                qwen_tts.synthesize("Vivian", "hi")
    assert local_stack.breaker_state("tts")["state"] == "closed"


def test_a_no_gpu_refusal_opens_the_breaker_at_once(stack_up):
    post, _ = _post_returning(NO_GPU)
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        r = qwen_tts.synthesize("Vivian", "hi")
    assert r.status_code == 503
    st = local_stack.breaker_state("tts")
    assert st["state"] == "open" and "nvidia-smi" in st["last_error"]


def test_a_transport_error_is_a_failure_and_still_raises(stack_up):
    post, _ = _post_returning(requests.exceptions.ConnectionError("refused"))
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        for _ in range(local_stack.BREAKER_THRESHOLD):
            with pytest.raises(requests.exceptions.ConnectionError):
                qwen_tts.synthesize("Vivian", "hi")
    assert local_stack.breaker_state("tts")["state"] == "open"


def test_a_success_closes_a_half_open_breaker(stack_up, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(local_stack, "_breaker_clock", lambda: clock[0])
    local_stack.report_failure("tts", "HTTP 500", fatal=True)
    clock[0] += local_stack.BREAKER_COOLDOWN_S
    post, _ = _post_returning(_R(200))
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        assert qwen_tts.synthesize("Vivian", "hi").status_code == 200
    assert local_stack.breaker_state("tts")["state"] == "closed"


def test_batch_no_gpu_raises_the_typed_capability_error_and_logs(stack_up, capsys):
    post, _ = _post_returning(NO_GPU)
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        with pytest.raises(qwen_tts.QwenCapabilityUnavailable) as exc:
            qwen_tts.synthesize_batch("Vivian", ["a", "b"])
    assert "HTTP 503" in str(exc.value) and "nvidia-smi" in str(exc.value)
    # journald (blackbox.service stdout) gets the member's reason: llama-swap
    # forwards none of the member's own logs.
    assert "[QWEN-TTS] synth failed HTTP 503" in capsys.readouterr().out


def test_batch_vram_503_stays_a_plain_retryable_runtime_error(stack_up):
    post, _ = _post_returning(VRAM)
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        with pytest.raises(RuntimeError) as exc:
            qwen_tts.synthesize_batch("Vivian", ["a"])
    assert not isinstance(exc.value, qwen_tts.QwenCapabilityUnavailable)


def test_batch_refuses_fast_when_the_gpu_is_dead(stack_up, monkeypatch):
    monkeypatch.setattr(local_stack, "gpu_ready", lambda: {
        "ok": False, "reason": "NVIDIA driver not responding"})
    post, posts = _post_returning(_R(200, {"wavs_b64": []}))
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=post):
        with pytest.raises(qwen_tts.QwenCapabilityUnavailable) as exc:
            qwen_tts.synthesize_batch("Vivian", ["a"])
    assert posts == [] and "NVIDIA driver not responding" in str(exc.value)
