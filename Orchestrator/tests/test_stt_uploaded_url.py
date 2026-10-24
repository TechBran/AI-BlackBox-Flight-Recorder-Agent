"""/stt transcribes a recording that /upload already stored, by its URL.

Audit W13: the Android recorder uploaded every take twice - to /upload for the
attachment URL, then the same bytes again to /stt for the transcript. /stt now
also takes a `url` form field naming a file under Portal/uploads (resolved by
resolve_upload_path, the shared containment check: nothing outside the
uploads folder, audio files only). A multipart `file` works exactly as before.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    from Orchestrator.utils import paths
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(paths, "uploads_dir", lambda: root)
    (root / "take.wav").write_bytes(b"RIFF-take-bytes")
    (root / "notes.txt").write_text("not audio")
    (tmp_path / "secret.wav").write_bytes(b"outside the uploads folder")
    return root


def test_stt_transcribes_an_uploaded_recording_by_url(app_client, uploads):
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes", return_value="hello") as m:
        r = app_client.post("/stt", data={"url": "/ui/uploads/take.wav"})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello"}
    args, kwargs = m.call_args
    assert args[0] == b"RIFF-take-bytes"
    assert args[1] == "audio/wav"
    assert kwargs["filename"] == "take.wav"


def test_stt_accepts_the_absolute_url_android_builds(app_client, uploads):
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes", return_value="hi") as m:
        r = app_client.post("/stt", data={"url": "https://box.tailnet.ts.net/ui/uploads/take.wav"})
    assert r.status_code == 200, r.text
    assert m.call_args.args[0] == b"RIFF-take-bytes"


@pytest.mark.parametrize("url, status", [
    ("/ui/uploads/../secret.wav", 400),
    ("/ui/uploads/notes.txt", 400),
    ("file:///etc/passwd", 400),
    ("/ui/uploads/missing.wav", 404),
])
def test_stt_refuses_urls_outside_the_uploaded_audio(app_client, uploads, url, status):
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes", return_value="x") as m:
        r = app_client.post("/stt", data={"url": url})
    assert r.status_code == status, r.text
    m.assert_not_called()


def test_stt_without_file_or_url_is_a_clear_400(app_client, uploads):
    r = app_client.post("/stt", data={"provider": "openai"})
    assert r.status_code == 400
    assert "file" in r.json()["detail"] and "url" in r.json()["detail"]


def test_stt_multipart_file_is_unchanged(app_client, uploads):
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes", return_value="flat") as m:
        r = app_client.post("/stt", files={"file": ("clip.webm", b"webm-bytes", "audio/webm")})
    assert r.status_code == 200 and r.json() == {"text": "flat"}
    assert m.call_args.args[:2] == (b"webm-bytes", "audio/webm")
    assert m.call_args.kwargs["filename"] == "clip.webm"
