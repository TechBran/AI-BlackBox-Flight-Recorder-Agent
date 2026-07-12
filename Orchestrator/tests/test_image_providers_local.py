"""Local (Z-Image) image adapter: request shape, credential resolution, registration."""
import base64
import pytest
from Orchestrator import image_providers
from Orchestrator.onboarding import custom_servers


class _Resp:
    def __init__(self, data):
        self._d = {"data": data}

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_local_adapter_posts_openai_images_shape(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Resp([{"b64_json": base64.b64encode(b"PNG").decode()}])

    monkeypatch.setattr(custom_servers, "resolve_image_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": "k"}, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", fake_post)

    out = image_providers._local_images("a fox", {"size": "768x768", "numberOfImages": 2})
    assert out == [b"PNG"]
    assert captured["url"] == "http://h/v1/images/generations"
    assert captured["json"] == {"model": "z-image", "prompt": "a fox",
                                "n": 2, "output_format": "png", "size": "768x768"}
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["timeout"] == 180


def test_local_adapter_no_size_omits_field(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured["json"] = json
        return _Resp([{"b64_json": base64.b64encode(b"P").decode()}])

    monkeypatch.setattr(custom_servers, "resolve_image_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": ""}, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._local_images("x", {})
    assert "size" not in captured["json"] and captured["json"]["n"] == 1


def test_local_adapter_keyless_omits_auth_header(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured["headers"] = headers
        return _Resp([{"b64_json": base64.b64encode(b"P").decode()}])

    monkeypatch.setattr(custom_servers, "resolve_image_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": ""}, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._local_images("x", {})
    assert "Authorization" not in captured["headers"]


def test_local_adapter_raises_when_no_server(monkeypatch):
    monkeypatch.setattr(custom_servers, "resolve_image_server", lambda model=None: None)
    with pytest.raises(RuntimeError):
        image_providers._local_images("x", {})


def test_local_registered():
    assert image_providers.IMAGE_PROVIDERS.get("local") is image_providers._local_images
    assert image_providers.IMAGE_TOOL_PROVIDERS.get("local_image") == "local"
