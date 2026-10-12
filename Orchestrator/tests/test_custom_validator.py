"""Hermetic tests for the custom OpenAI-compatible server validator.

Network is mocked at the SOURCE (openai.OpenAI) — validate_custom does
`from openai import OpenAI` inside its body, so patching the module attribute
intercepts the client at call time (patching the validators namespace would
AttributeError). Errors are raised as the REAL openai SDK exception types,
constructed with httpx request/response objects, so the validator's except
clauses are exercised exactly as in production.
"""
import httpx
import openai

from Orchestrator.onboarding import validators


class _FakeModel:
    def __init__(self, model_id: str):
        self.id = model_id


class _FakeModelsPage:
    def __init__(self, ids: list[str]):
        self.data = [_FakeModel(i) for i in ids]


class _FakeModels:
    def __init__(self, ids: list[str] | None = None, error: Exception | None = None):
        self._ids = ids or []
        self._error = error

    def list(self):
        if self._error is not None:
            raise self._error
        return _FakeModelsPage(self._ids)


def _fake_client_factory(ids: list[str] | None = None,
                         error: Exception | None = None,
                         captured: dict | None = None):
    """Build a FakeClient class mimicking openai.OpenAI (context manager + models.list)."""

    class _FakeClient:
        def __init__(self, api_key=None, base_url=None, timeout=None,
                     max_retries=None, **kwargs):
            if captured is not None:
                captured.update(api_key=api_key, base_url=base_url,
                                timeout=timeout, max_retries=max_retries)
            self.models = _FakeModels(ids=ids, error=error)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _FakeClient


# --- validate_custom ----------------------------------------------------------

def test_validate_custom_ok_returns_models(monkeypatch):
    """models.list success -> ok + {model_count, models} detail (doubles as discovery)."""
    captured = {}
    monkeypatch.setattr(
        "openai.OpenAI",
        _fake_client_factory(ids=["gemma-26b", "gemma-12b"], captured=captured),
    )

    res = validators.validate_custom(base_url="http://192.168.1.50:8080/v1", api_key="k")
    assert res.ok is True
    assert res.error is None
    assert res.detail is not None
    assert res.detail["model_count"] == 2
    assert "gemma-26b" in res.detail["models"]
    assert isinstance(res.latency_ms, int)
    # Client wired to the user-supplied server with LAN-probe settings.
    assert captured["base_url"] == "http://192.168.1.50:8080/v1"
    assert captured["api_key"] == "k"
    assert captured["max_retries"] == 0


def test_validate_custom_keyless_server_sends_placeholder_key(monkeypatch):
    """The openai SDK refuses empty keys; keyless LAN servers get api_key='none'."""
    captured = {}
    monkeypatch.setattr(
        "openai.OpenAI",
        _fake_client_factory(ids=["gemma-26b"], captured=captured),
    )

    res = validators.validate_custom(base_url="http://x/v1")
    assert res.ok is True
    assert captured["api_key"] == "none"


def test_validate_custom_caps_models_list_at_50(monkeypatch):
    """model_count reports the TRUE total; the ids list is capped at 50."""
    ids = [f"model-{i}" for i in range(60)]
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(ids=ids))

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok is True
    assert res.detail["model_count"] == 60
    assert len(res.detail["models"]) == 50


def test_validate_custom_auth_error(monkeypatch):
    """Real AuthenticationError -> ok False, error tells the user the key was rejected."""
    req = httpx.Request("GET", "http://x/v1/models")
    err = openai.AuthenticationError(
        "Error code: 401 - invalid api key",
        response=httpx.Response(401, request=req),
        body=None,
    )
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(error=err))

    res = validators.validate_custom(base_url="http://x/v1", api_key="bad")
    assert res.ok is False
    assert res.detail is None
    assert res.error is not None
    assert "API key rejected (401)" in res.error


def test_validate_custom_http_status_error_hints_at_v1_path(monkeypatch):
    """Real NotFoundError (APIStatusError subclass, 404) -> ok False with the
    classic missing-/v1 hint. AuthenticationError (401) must still win — its
    except clause is ordered first (it's also an APIStatusError subclass)."""
    req = httpx.Request("GET", "http://192.168.1.50:8080/models")
    err = openai.NotFoundError(
        "Error code: 404 - not found",
        response=httpx.Response(404, request=req),
        body=None,
    )
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(error=err))

    # base_url deliberately missing /v1 — the hint must come from the error text.
    res = validators.validate_custom(base_url="http://192.168.1.50:8080", api_key="k")
    assert res.ok is False
    assert res.detail is None
    assert res.error is not None
    assert "404" in res.error
    assert "/v1" in res.error


def test_validate_custom_unreachable(monkeypatch):
    """Real APIConnectionError -> ok False, error names the unreachable base_url."""
    req = httpx.Request("GET", "http://192.168.1.99:8080/v1/models")
    err = openai.APIConnectionError(request=req)
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(error=err))

    res = validators.validate_custom(base_url="http://192.168.1.99:8080/v1", api_key="")
    assert res.ok is False
    assert res.detail is None
    assert res.error is not None
    assert "Server unreachable at http://192.168.1.99:8080/v1" in res.error


def test_validate_custom_never_raises_on_other_error(monkeypatch):
    """Any other exception is captured by _measure as ok False, never propagated."""
    monkeypatch.setattr(
        "openai.OpenAI",
        _fake_client_factory(error=ValueError("unexpected payload shape")),
    )

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok is False
    assert res.error is not None
    assert "unexpected payload shape" in res.error


# --- audio capability probe: tri-state ----------------------------------------
# validate_custom does `import httpx` inside its body, so patching the module
# attribute intercepts the probe at call time (same seam as test_local_audio).


class _StubResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _probe_stub(outcomes: dict):
    """httpx.get stub: {path fragment: status code | Exception to raise}; 404 default."""
    def _get(url, **kwargs):
        for frag, outcome in outcomes.items():
            if frag in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return _StubResponse(outcome)
        return _StubResponse(404)
    return _get


def test_audio_probe_definite_404_is_false(monkeypatch):
    """A real 404 is real evidence of absence -> False."""
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(ids=["m"]))
    monkeypatch.setattr(httpx, "get", _probe_stub({}))

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok is True
    assert res.detail["audio"] == {"stt": False, "tts": False, "streaming": False}


def test_audio_probe_definite_response_is_true(monkeypatch):
    """Any non-404 response (200/405/307) proves the path exists -> True."""
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(ids=["m"]))
    monkeypatch.setattr(httpx, "get", _probe_stub({
        "/audio/transcriptions": 200,
        "/audio/speech": 405,
        "/realtime": 307,
    }))

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok is True
    assert res.detail["audio"] == {"stt": True, "tts": True, "streaming": True}


def test_audio_probe_exception_is_none_not_false(monkeypatch):
    """A timeout / connection reset is NOT evidence of absence -> None.

    Returning False here is what wrote audio.streaming=false for a server whose
    /realtime answers 307, silently disabling live-mic local STT.
    """
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(ids=["m"]))
    monkeypatch.setattr(httpx, "get", _probe_stub({
        "/audio/transcriptions": httpx.ConnectTimeout("timed out"),
        "/audio/speech": httpx.ReadTimeout("timed out"),
        "/realtime": httpx.ConnectError("connection reset by peer"),
    }))

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok is True
    assert res.detail["audio"] == {"stt": None, "tts": None, "streaming": None}


def test_audio_probe_states_are_per_path_independent(monkeypatch):
    """One inconclusive probe must not contaminate the paths that DID answer."""
    monkeypatch.setattr("openai.OpenAI", _fake_client_factory(ids=["m"]))
    monkeypatch.setattr(httpx, "get", _probe_stub({
        "/audio/transcriptions": 405,
        "/audio/speech": 404,
        "/realtime": httpx.ReadTimeout("timed out"),
    }))

    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.detail["audio"] == {"stt": True, "tts": False, "streaming": None}
