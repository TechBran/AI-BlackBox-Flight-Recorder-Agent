"""Hermetic tests for ElevenLabs Music (POST /v1/music) + its task processor.

No network, no live key: ``requests.post`` is monkeypatched into a recorder so we
can assert the exact URL/params/body the API would have received, and auth headers
are stubbed so no key lookup hits ``.env``. The processor test additionally mocks
``compose`` + disk so it stores a result without touching the API or filesystem.
"""
import pytest

from Orchestrator import config
from Orchestrator.elevenlabs import client as el_client
from Orchestrator.elevenlabs import music


class _FakeResp:
    """Minimal stand-in for requests.Response: status + bytes + JSON body."""
    def __init__(self, status_code, content=b"", json_body=None):
        self.status_code = status_code
        self.content = content
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture(autouse=True)
def _stub_auth(monkeypatch):
    """Never touch a real key; auth_headers returns a fixed fake header."""
    monkeypatch.setattr(el_client, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield


def _record_post(monkeypatch, responses):
    """Patch music.requests.post to pop canned responses; return the call recorder."""
    calls = []

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
        return responses[len(calls) - 1]

    monkeypatch.setattr(music.requests, "post", fake_post)
    return calls


# --- compose() : prompt path ------------------------------------------------

def test_compose_with_prompt_posts_prompt_and_length(monkeypatch):
    """prompt + music_length_ms -> both on the wire; default format; bytes back."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"MP3")])

    out = music.compose(prompt="gentle lo-fi", music_length_ms=8000)

    assert out == b"MP3"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/v1/music")
    assert calls[0]["json"]["prompt"] == "gentle lo-fi"
    assert calls[0]["json"]["music_length_ms"] == 8000
    assert "composition_plan" not in calls[0]["json"]
    # default format is config-backed (env-overridable), and 128 (long-song tier).
    assert calls[0]["params"]["output_format"] == config.ELEVENLABS_MUSIC_FORMAT_DEFAULT
    assert config.ELEVENLABS_MUSIC_FORMAT_DEFAULT == "mp3_44100_128"


def test_compose_optional_fields_only_sent_when_provided(monkeypatch):
    """force_instrumental/seed/length absent unless set; force=False stays out."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A"), _FakeResp(200, content=b"B")])

    music.compose(prompt="x")
    assert calls[0]["json"] == {"prompt": "x"}  # nothing extra leaks in

    music.compose(prompt="y", music_length_ms=30000, force_instrumental=True, seed=42)
    body = calls[1]["json"]
    assert body["music_length_ms"] == 30000
    assert body["force_instrumental"] is True
    assert body["seed"] == 42


def test_compose_output_format_override(monkeypatch):
    """Explicit output_format reaches the query string."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A")])
    music.compose(prompt="x", output_format="mp3_44100_192")
    assert calls[0]["params"]["output_format"] == "mp3_44100_192"


# --- compose() : composition_plan path -------------------------------------

def test_compose_with_composition_plan_posts_plan(monkeypatch):
    """composition_plan -> on the wire; prompt absent."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"PLAN")])
    plan = {"sections": [{"name": "verse", "lines": ["la la"]}]}

    out = music.compose(composition_plan=plan)

    assert out == b"PLAN"
    assert calls[0]["json"]["composition_plan"] == plan
    assert "prompt" not in calls[0]["json"]


# --- compose() : XOR enforcement -------------------------------------------

def test_compose_both_raises_value_error(monkeypatch):
    """prompt AND composition_plan -> ValueError, no POST."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"x")])
    with pytest.raises(ValueError):
        music.compose(prompt="x", composition_plan={"sections": []})
    assert len(calls) == 0


def test_compose_neither_raises_value_error(monkeypatch):
    """Neither prompt nor composition_plan -> ValueError, no POST."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"x")])
    with pytest.raises(ValueError):
        music.compose()
    assert len(calls) == 0


# --- compose() : error mapping ---------------------------------------------

def test_compose_4xx_raises_mapped_runtime_error(monkeypatch):
    """A 401 -> RuntimeError carrying the mapped auth message."""
    _record_post(monkeypatch, [_FakeResp(401, json_body={"detail": {"status": "auth_error"}})])
    with pytest.raises(RuntimeError) as ei:
        music.compose(prompt="x")
    assert "auth" in str(ei.value).lower()


def test_compose_4xx_non_json_body_still_maps(monkeypatch):
    """Error body that isn't JSON -> still a RuntimeError (defensive parse)."""
    _record_post(monkeypatch, [_FakeResp(422, json_body=None)])
    with pytest.raises(RuntimeError):
        music.compose(prompt="x")


# --- task processor ---------------------------------------------------------

def test_process_elevenlabs_music_calls_compose_and_stores_result(monkeypatch, tmp_path):
    """process_elevenlabs_music: compose() bytes -> saved mp3 + COMPLETED result.

    compose + disk + media-index are all mocked, so this asserts the wiring
    (params passed through, file written, result_url set) with no API/FS deps.
    """
    from Orchestrator import tasks as tasks_module
    from Orchestrator.elevenlabs import music as el_music
    from Orchestrator.models import Task, TaskStatus, TaskType
    from Orchestrator.volume import now_utc_iso

    # Redirect the uploads dir to a tmp path (processor uses UPLOADS_DIR / filename).
    monkeypatch.setattr(tasks_module, "UPLOADS_DIR", tmp_path)
    # Don't touch the real media index.
    monkeypatch.setattr(tasks_module, "add_media_entry", lambda **kw: None)

    # Capture compose() args; return fixed bytes instead of calling the API.
    captured = {}

    def fake_compose(**kwargs):
        captured.update(kwargs)
        return b"ID3FAKEMP3BYTES"

    monkeypatch.setattr(el_music, "compose", fake_compose)

    # Capture update_task calls (the processor reports progress + final status here).
    updates = []
    monkeypatch.setattr(tasks_module, "update_task", lambda task_id, **kw: updates.append(kw))

    now = now_utc_iso()
    task = Task(
        task_id="test-elm-1",
        task_type=TaskType.ELEVENLABS_MUSIC,
        status=TaskStatus.PROCESSING,
        created_at=now,
        updated_at=now,
        prompt="gentle lo-fi",
        result_data={"prompt": "gentle lo-fi", "music_length_ms": 8000, "force_instrumental": False},
    )

    tasks_module.process_elevenlabs_music(task)

    # compose() got the prompt + length from the task params.
    assert captured["prompt"] == "gentle lo-fi"
    assert captured["music_length_ms"] == 8000
    assert captured["force_instrumental"] is False

    # An mp3 was written to the uploads dir with the task id in the name.
    written = list(tmp_path.glob("*.mp3"))
    assert len(written) == 1
    assert "test-elm-1" in written[0].name
    assert written[0].read_bytes() == b"ID3FAKEMP3BYTES"

    # Final update marked the task COMPLETED with a /ui/uploads/ result_url.
    final = updates[-1]
    assert final["status"] == TaskStatus.COMPLETED
    assert final["result_url"].startswith("/ui/uploads/")
    assert final["result_url"].endswith(".mp3")
