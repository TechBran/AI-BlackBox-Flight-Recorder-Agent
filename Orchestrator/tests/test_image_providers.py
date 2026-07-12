"""Tests for per-provider image adapters + worker routing.

NO real API calls -- requests.post/get and IMAGE_PROVIDERS are monkeypatched.
"""
import base64

import pytest

from Orchestrator import image_providers
from Orchestrator import tasks
from Orchestrator.models import Task, TaskType, TaskStatus


class _FakeResp:
    def __init__(self, json_data=None, content=None):
        self._json = json_data or {}
        self.content = content or b""

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def test_openai_images_b64(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        b64 = base64.b64encode(b"PNG").decode()
        return _FakeResp(json_data={"data": [{"b64_json": b64}]})

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    out = image_providers._openai_images("a cat", {"numberOfImages": 1})
    assert out == [b"PNG"]
    assert captured["url"] == image_providers.OPENAI_IMAGES_URL
    assert captured["json"]["model"] == image_providers.OPENAI_IMAGE_MODEL
    assert captured["json"]["n"] == 1


def test_openai_images_passes_size_quality(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        b64 = base64.b64encode(b"X").decode()
        return _FakeResp(json_data={"data": [{"b64_json": b64}]})

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._openai_images("a cat", {"n": 2, "size": "1024x1024", "quality": "high"})
    assert captured["json"]["n"] == 2
    assert captured["json"]["size"] == "1024x1024"
    assert captured["json"]["quality"] == "high"


def test_xai_images_url_path(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(json_data={"data": [{"url": "http://x/y"}]})

    def fake_get(url, timeout=None):
        assert url == "http://x/y"
        return _FakeResp(content=b"IMG")

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    monkeypatch.setattr(image_providers.requests, "get", fake_get)
    out = image_providers._xai_images("a dog", {"numberOfImages": 1})
    assert out == [b"IMG"]


def test_xai_images_b64_path(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        b64 = base64.b64encode(b"GROKPNG").decode()
        return _FakeResp(json_data={"data": [{"b64_json": b64}]})

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    out = image_providers._xai_images("a dog", {})
    assert out == [b"GROKPNG"]


def _make_task(options):
    return Task(
        task_id="t-test-123",
        task_type=TaskType.IMAGE_GENERATION,
        status=TaskStatus.PROCESSING,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        prompt="a red apple",
        result_data={"options": options},
    )


def _route_harness(monkeypatch, tmp_path, options):
    """Run process_image_generation with all side-effects stubbed; return calls."""
    calls = {"provider_calls": [], "media": [], "updates": []}

    def make_stub(tag):
        def stub(prompt, opts):
            calls["provider_calls"].append(tag)
            if tag == "local":
                # mirror _local_images: surface the resolved model for provenance
                opts["_resolved_image_model"] = "flux.2-klein-4b"
            return [b"BYTES"]
        return stub

    fake_providers = {
        "gemini": make_stub("gemini"),
        "openai": make_stub("openai"),
        "grok": make_stub("grok"),
        "local": make_stub("local"),
    }
    monkeypatch.setattr(tasks, "IMAGE_PROVIDERS", fake_providers, raising=False)
    monkeypatch.setattr(tasks, "DEFAULT_IMAGE_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(tasks, "generate_prompt_slug", lambda p: "slug")
    monkeypatch.setattr(tasks, "add_media_entry", lambda **kw: calls["media"].append(kw))

    def fake_update(task_id, **kw):
        calls["updates"].append(kw)

    monkeypatch.setattr(tasks, "update_task", fake_update)
    # neutralize env so default-provider tests are deterministic
    monkeypatch.delenv("IMAGE_DEFAULT", raising=False)

    task = _make_task(options)
    tasks.process_image_generation(task)
    return calls


def test_routing_openai(monkeypatch, tmp_path):
    calls = _route_harness(monkeypatch, tmp_path, {"provider": "openai"})
    assert calls["provider_calls"] == ["openai"]
    # a file was written
    written = list(tmp_path.glob("*.png"))
    assert len(written) == 1
    assert written[0].read_bytes() == b"BYTES"
    # url recorded in result/media
    assert calls["media"]
    assert calls["media"][0]["url"].startswith("/ui/uploads/")
    # COMPLETED update carries all_urls
    completed = [u for u in calls["updates"] if u.get("status") == TaskStatus.COMPLETED]
    assert completed
    assert completed[0]["result_data"]["all_urls"]


def test_routing_grok(monkeypatch, tmp_path):
    calls = _route_harness(monkeypatch, tmp_path, {"provider": "grok"})
    assert calls["provider_calls"] == ["grok"]
    assert list(tmp_path.glob("*.png"))


def test_routing_untagged_defaults(monkeypatch, tmp_path):
    calls = _route_harness(monkeypatch, tmp_path, {})
    assert calls["provider_calls"] == ["gemini"]
    assert list(tmp_path.glob("*.png"))


def test_routing_local(monkeypatch, tmp_path):
    calls = _route_harness(monkeypatch, tmp_path, {"provider": "local", "size": "768x768"})
    assert calls["provider_calls"] == ["local"]
    assert list(tmp_path.glob("*.png"))
    meta = calls["media"][0]["extra_metadata"]
    # provenance uses the adapter-RESOLVED model, not the hardcoded _IMAGE_MODELS default
    assert meta["model"] == "flux.2-klein-4b"
    assert meta["size"] == "768x768"
