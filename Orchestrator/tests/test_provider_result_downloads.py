# Orchestrator/tests/test_provider_result_downloads.py
"""Provider-returned URLs are fetched on the box's behalf (audit M3 U20 item 2).

Image providers can answer with a URL instead of inline bytes, and Veo answers
with a download URI that the box fetches WITH the Google API key. A hostile or
broken response used to steer a bare requests.get anywhere: the box's own
loopback services (:9091 the API, :9093 MCP, :9098 the model proxy), the LAN,
cloud metadata, and for Veo it would hand the key to that host too.

  * cloud image URLs (xAI) go through the SSRF guard (utils.safe_fetch):
    public internet only, every redirect re-vetted, body capped;
  * a LAN image server (the `local` provider) may only point back at its own
    registered origin, with no redirects and a capped body;
  * Veo's download URI must be https on the API host the key belongs to.

Hermetic: requests and the guard's transport are faked; refused URLs are
refused before any socket is opened.
"""
import pytest

from Orchestrator import image_providers
from Orchestrator.onboarding import custom_servers


class _PostResp:
    def __init__(self, data):
        self._d = {"data": data}

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _no_bare_get(calls):
    def fake(url, *a, **k):
        calls.append(url)
        raise AssertionError(f"bare requests.get({url!r}) — must go through a guard")
    return fake


# ----------------------------------------------------------------- xAI (cloud)

@pytest.mark.parametrize("hostile", [
    "http://127.0.0.1:9091/agent/apps",
    "http://localhost:9093/internal/reload",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.153:9098/v1/models",
    "http://100.101.102.103:9091/",
    "http://0x7f000001:9091/",
    "file:///etc/passwd",
])
def test_xai_hostile_image_url_is_refused_before_any_fetch(monkeypatch, hostile):
    calls = []
    monkeypatch.setattr(image_providers.requests, "post",
                        lambda *a, **k: _PostResp([{"url": hostile}]))
    monkeypatch.setattr(image_providers.requests, "get", _no_bare_get(calls))
    with pytest.raises(Exception) as exc:
        image_providers._xai_images("p", {})
    assert not isinstance(exc.value, AssertionError), exc.value
    assert calls == []


def test_xai_public_image_url_goes_through_the_guard(monkeypatch):
    from Orchestrator.utils.safe_fetch import FetchResult

    seen = {}

    def fake_safe_get(url, **kw):
        seen.update(url=url, **kw)
        return FetchResult(url=url, status_code=200, headers={}, content=b"IMG")

    monkeypatch.setattr(image_providers.requests, "post",
                        lambda *a, **k: _PostResp([{"url": "https://imgen.x.ai/t/1.png"}]))
    monkeypatch.setattr(image_providers.requests, "get", _no_bare_get([]))
    monkeypatch.setattr(image_providers, "safe_get", fake_safe_get)
    assert image_providers._xai_images("p", {}) == [b"IMG"]
    assert seen["url"] == "https://imgen.x.ai/t/1.png"
    assert seen["max_bytes"] == image_providers.IMAGE_DOWNLOAD_MAX_BYTES


def test_xai_image_download_error_status_fails_loudly(monkeypatch):
    from Orchestrator.utils.safe_fetch import FetchResult

    monkeypatch.setattr(image_providers.requests, "post",
                        lambda *a, **k: _PostResp([{"url": "https://imgen.x.ai/t/1.png"}]))
    monkeypatch.setattr(image_providers, "safe_get",
                        lambda url, **kw: FetchResult(url=url, status_code=404, headers={}, content=b"no"))
    with pytest.raises(RuntimeError, match="404"):
        image_providers._xai_images("p", {})


# --------------------------------------------------------- local (LAN server)

SERVER = {"base_url": "http://192.168.1.50:9098/v1", "api_key": "k"}


class _GetResp:
    def __init__(self, status=200, body=b"PNG", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), 7):
            yield self._body[i:i + 7]

    def close(self):
        self.closed = True


def _local(monkeypatch, url, get_resp=None):
    seen = []
    monkeypatch.setattr(custom_servers, "resolve_image_server", lambda model=None: (SERVER, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", lambda *a, **k: _PostResp([{"url": url}]))

    def fake_get(u, **kw):
        seen.append((u, kw))
        return get_resp or _GetResp()

    monkeypatch.setattr(image_providers.requests, "get", fake_get)
    return seen


@pytest.mark.parametrize("hostile", [
    "http://127.0.0.1:9091/agent/apps",           # the box's own API
    "http://192.168.1.50:9091/agent/apps",        # same host, another service
    "http://192.168.1.51:9098/out/1.png",         # another LAN host
    "https://192.168.1.50:9098/out/1.png",        # another scheme
    "http://user:pw@192.168.1.50:9098/out/1.png",
    "//169.254.169.254/latest/meta-data/",        # scheme-relative escape
    "file:///etc/passwd",
    "http://192.168.1.50:99999/out/1.png",        # bad port
    12345,                                        # not a string at all
])
def test_local_server_url_must_stay_on_its_own_origin(monkeypatch, hostile):
    seen = _local(monkeypatch, hostile)
    with pytest.raises(RuntimeError):
        image_providers._local_images("p", {})
    assert seen == []


@pytest.mark.parametrize("url, fetched", [
    ("http://192.168.1.50:9098/out/1.png", "http://192.168.1.50:9098/out/1.png"),
    ("/out/2.png", "http://192.168.1.50:9098/out/2.png"),          # root-relative
    ("out/3.png", "http://192.168.1.50:9098/v1/out/3.png"),        # path-relative
    ("HTTP://192.168.1.50:9098/out/4.png", "http://192.168.1.50:9098/out/4.png"),
])
def test_local_server_own_urls_still_download(monkeypatch, url, fetched):
    seen = _local(monkeypatch, url)
    assert image_providers._local_images("p", {}) == [b"PNG"]
    assert seen[0][0] == fetched
    assert seen[0][1]["allow_redirects"] is False
    assert seen[0][1]["stream"] is True


def test_local_server_redirect_is_refused(monkeypatch):
    _local(monkeypatch, "/out/1.png",
           _GetResp(status=302, body=b"", headers={"Location": "http://127.0.0.1:9091/"}))
    with pytest.raises(RuntimeError, match="redirect"):
        image_providers._local_images("p", {})


def test_local_server_oversized_body_is_refused(monkeypatch):
    monkeypatch.setattr(image_providers, "IMAGE_DOWNLOAD_MAX_BYTES", 20)
    resp = _GetResp(body=b"x" * 50)
    _local(monkeypatch, "/out/1.png", resp)
    with pytest.raises(RuntimeError, match="larger"):
        image_providers._local_images("p", {})
    assert resp.closed


# ------------------------------------------------------------- Veo download

def test_veo_download_uri_must_be_https_on_the_api_host():
    from Orchestrator.tasks import _vet_veo_download_uri

    api = "https://generativelanguage.googleapis.com/v1beta"
    ok = "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"
    assert _vet_veo_download_uri(ok, api) == ok
    for bad in ("http://generativelanguage.googleapis.com/v1beta/files/abc:download",
                "https://evil.example/files/abc",
                "https://generativelanguage.googleapis.com.evil.example/x",
                "https://user@generativelanguage.googleapis.com/x",
                "http://127.0.0.1:9091/agent/apps",
                "", None, 42):
        with pytest.raises(ValueError):
            _vet_veo_download_uri(bad, api)


def test_veo_hostile_download_uri_never_gets_the_key(monkeypatch):
    from Orchestrator import tasks
    from Orchestrator.models import Task, TaskStatus, TaskType

    got = []

    class _R:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._b

    def fake_get(url, headers=None, **kw):
        got.append((url, headers))
        if url.endswith("operations/op-1"):
            return _R({"done": True, "response": {"generateVideoResponse": {
                "generatedSamples": [{"video": {"uri": "http://127.0.0.1:9091/agent/apps"}}]}}})
        raise AssertionError("downloaded a hostile URI")

    monkeypatch.setattr(tasks, "GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(tasks, "update_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "append_task_progress", lambda *a, **k: None)
    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    monkeypatch.setattr(tasks.requests, "post", lambda *a, **k: _R({"name": "operations/op-1"}))
    monkeypatch.setattr(tasks.requests, "get", fake_get)

    task = Task(task_id="t-veo", task_type=TaskType.VIDEO_GENERATION, status=TaskStatus.PROCESSING,
                created_at="2026-10-17T00:00:00Z", updated_at="2026-10-17T00:00:00Z", prompt="p",
                result_data={"options": {}})
    with pytest.raises(Exception) as exc:
        tasks.process_video_generation(task)
    assert not isinstance(exc.value, AssertionError), exc.value
    assert [u for u, _ in got if "127.0.0.1" in u] == []
