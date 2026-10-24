"""Veo's durationSeconds must go out as a number.

MS02 production logs, 2026-10-17..23: predictLongRunning failed with HTTP 400
"The value type for durationSeconds needs to be a number". Model tool calls
often send `duration` as a string ("8", "8s"), every chat loop copies it into
the task options unchanged, and process_video_generation put it straight into
the request. The same string also failed the 1080p-needs-8s check ("8" != 8),
so a valid 1080p request was silently downgraded to 720p.

Hermetic: requests.post captures the body and stops the run.
"""
import pytest

from Orchestrator import tasks
from Orchestrator.models import Task, TaskStatus, TaskType


class _Stop(Exception):
    pass


def _payload(monkeypatch, options):
    seen = {}
    monkeypatch.setattr(tasks, "GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(tasks, "update_task", lambda *a, **k: None)

    def capture(url, **kw):
        seen["json"] = kw.get("json")
        raise _Stop()

    monkeypatch.setattr(tasks.requests, "post", capture)
    task = Task(task_id="t-veo", task_type=TaskType.VIDEO_GENERATION, status=TaskStatus.PROCESSING,
                created_at="2026-10-23T00:00:00Z", updated_at="2026-10-23T00:00:00Z",
                prompt="waves on rocks", result_data={"options": options})
    with pytest.raises(_Stop):
        tasks.process_video_generation(task)
    return seen["json"]


@pytest.mark.parametrize("raw, seconds", [
    (8, 8), ("8", 8), (" 6 ", 6), ("4s", 4), ("8 seconds", 8), (6.0, 6), ("6.0", 6),
])
def test_duration_seconds_is_a_number(monkeypatch, raw, seconds):
    params = _payload(monkeypatch, {"duration": raw})["parameters"]
    assert params["durationSeconds"] == seconds
    assert type(params["durationSeconds"]) is int


@pytest.mark.parametrize("raw", [None, "", "long", True, [8]])
def test_an_unreadable_duration_falls_back_to_the_default(monkeypatch, raw):
    params = _payload(monkeypatch, {"duration": raw})["parameters"]
    assert params["durationSeconds"] == 8


def test_missing_duration_is_the_default(monkeypatch):
    assert _payload(monkeypatch, {})["parameters"]["durationSeconds"] == 8


def test_a_string_8_keeps_1080p(monkeypatch):
    params = _payload(monkeypatch, {"duration": "8", "resolution": "1080p"})["parameters"]
    assert params["resolution"] == "1080p"


def test_1080p_with_another_duration_still_falls_back(monkeypatch):
    params = _payload(monkeypatch, {"duration": "6", "resolution": "1080p"})["parameters"]
    assert params["resolution"] == "720p"
