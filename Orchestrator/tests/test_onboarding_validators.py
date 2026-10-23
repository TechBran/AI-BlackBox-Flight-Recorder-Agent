"""Hermetic tests for onboarding key validators.

Network is mocked at requests.get so no live call is ever made. The live
curls in the wizard/plan are the separate manual verification step.
"""
import pytest

from Orchestrator.onboarding import validators


class _FakeResponse:
    """Minimal stand-in for requests.Response covering what validators touch."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


# --- validate_elevenlabs ------------------------------------------------------

def test_validate_elevenlabs_valid_key_surfaces_tier_and_cloning(monkeypatch):
    """200 + Starter subscription -> ok, tier, credits, cloning-available string."""
    payload = {
        "subscription": {
            "tier": "starter",
            "can_use_instant_voice_cloning": True,
            "character_limit": 90000,
            "character_count": 0,
        }
    }

    def _fake_get(url, headers=None, timeout=None, **kwargs):
        assert url == "https://api.elevenlabs.io/v1/user"
        assert headers == {"xi-api-key": "xi-good"}
        return _FakeResponse(200, payload)

    monkeypatch.setattr("requests.get", _fake_get)

    result = validators.validate_elevenlabs("xi-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["tier"] == "starter"
    assert result.detail["credits_remaining"] == 90000
    assert "cloning available" in result.detail["features"]


def test_validate_elevenlabs_no_cloning_on_plan(monkeypatch):
    """can_use_instant_voice_cloning False -> 'not available' string (explicit boolean,
    not tier-name inference)."""
    payload = {
        "subscription": {
            "tier": "free",
            "can_use_instant_voice_cloning": False,
            "character_limit": 10000,
            "character_count": 2500,
        }
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(200, payload),
    )

    result = validators.validate_elevenlabs("xi-free")
    assert result.ok is True
    assert result.detail["tier"] == "free"
    assert result.detail["credits_remaining"] == 7500
    assert "not available" in result.detail["features"]


def test_validate_elevenlabs_invalid_key_401(monkeypatch):
    """401 -> ok False with an error message mentioning the bad key."""
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(401, {"detail": "unauthorized"}),
    )

    result = validators.validate_elevenlabs("xi-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid ElevenLabs API key" in result.error
    assert result.detail is None


def test_validate_elevenlabs_missing_subscription_defaults(monkeypatch):
    """200 with no subscription block -> defaults (tier free, 0 credits) without raising."""
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(200, {}),
    )

    result = validators.validate_elevenlabs("xi-empty")
    assert result.ok is True
    assert result.detail["tier"] == "free"
    assert result.detail["credits_remaining"] == 0


# --- validate_cohere (M10.0) --------------------------------------------------

def test_validate_cohere_valid_key_surfaces_org(monkeypatch):
    """200 from the zero-cost check-api-key endpoint -> ok + organization detail."""
    def _fake_post(url, headers=None, timeout=None, **kwargs):
        assert url == "https://api.cohere.ai/v1/check-api-key"
        assert headers == {"Authorization": "Bearer co-good"}
        return _FakeResponse(200, {"valid": True, "organization_name": "Acme Corp"})

    monkeypatch.setattr("requests.post", _fake_post)

    result = validators.validate_cohere("co-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["organization"] == "Acme Corp"


def test_validate_cohere_invalid_key_401(monkeypatch):
    """401 -> ok False with a clear error, no detail, never raises."""
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(401, {"message": "invalid api token"}),
    )
    result = validators.validate_cohere("co-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid Cohere API key" in result.error
    assert result.detail is None


def test_validate_cohere_never_raises_on_transport_error(monkeypatch):
    """A transport exception is captured as ok False, never propagated."""
    def _boom(*a, **k):
        raise ConnectionError("dns failure")

    monkeypatch.setattr("requests.post", _boom)
    result = validators.validate_cohere("co-x")
    assert result.ok is False
    assert result.error is not None


# --- validate_voyage (M10.0) --------------------------------------------------

def test_validate_voyage_valid_key_uses_one_document_probe(monkeypatch):
    """200 from a tiny 1-document rerank -> ok. The probe MUST send exactly one
    document (stays under the free-tier 10K-TPM cap a 40-doc call exceeds)."""
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        assert url == "https://api.voyageai.com/v1/rerank"
        assert headers["Authorization"] == "Bearer pa-good"
        captured["json"] = json
        return _FakeResponse(200, {"model": "rerank-2.5",
                                   "data": [{"index": 0, "relevance_score": 0.9}]})

    monkeypatch.setattr("requests.post", _fake_post)

    result = validators.validate_voyage("pa-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["model"] == "rerank-2.5"
    # 1-document discipline (the M8 free-tier finding).
    assert len(captured["json"]["documents"]) == 1


def test_validate_voyage_invalid_key_401(monkeypatch):
    """401 -> ok False with a clear error, no detail, never raises."""
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(401, {"detail": "Provided API key is invalid."}),
    )
    result = validators.validate_voyage("pa-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid Voyage API key" in result.error
    assert result.detail is None


def test_validate_voyage_never_raises_on_transport_error(monkeypatch):
    """A transport exception is captured as ok False, never propagated."""
    def _boom(*a, **k):
        raise ConnectionError("timeout")

    monkeypatch.setattr("requests.post", _boom)
    result = validators.validate_voyage("pa-x")
    assert result.ok is False
    assert result.error is not None


# --- probe ids come from live discovery / config, never a literal (O16-12) ----
#
# A validator that probes with a hard-coded model id turns a VALID key into
# "invalid" the day the provider retires that id (the repo was hit by this
# class in 6071f260). Anthropic and xAI are probed with the zero-cost
# models.list(); Perplexity and Voyage have no list endpoint, so their probe id
# comes from the same config the web-search and rerank layers send.

class _FakeModelsPage:
    def __init__(self, ids):
        self.data = [type("M", (), {"id": i})() for i in ids]


class _FakeAnthropic:
    """anthropic.Anthropic stand-in: models.list() works, messages.create is a
    test failure (it costs tokens and needs a model id)."""
    seen: dict = {}

    def __init__(self, api_key=None, **kwargs):
        _FakeAnthropic.seen = {"api_key": api_key, **kwargs}
        fail = api_key == "sk-ant-bad"

        class _Models:
            def list(self, **kw):
                if fail:
                    raise RuntimeError("401 authentication_error: invalid x-api-key")
                return _FakeModelsPage(["model-a", "model-b", "model-c"])

        class _Messages:
            def create(self, **kw):
                raise AssertionError(f"key probe must not send a message (model={kw.get('model')})")

        self.models = _Models()
        self.messages = _Messages()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpenAI:
    """openai.OpenAI stand-in recording base_url and every chat call."""
    instances: list = []

    def __init__(self, api_key=None, base_url=None, **kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.chat_calls = []
        _FakeOpenAI.instances.append(self)
        outer = self

        class _Models:
            def list(self, **kw):
                if outer.api_key == "bad":
                    raise RuntimeError("401 Incorrect API key provided")
                return _FakeModelsPage(["grok-x", "grok-y"])

        class _Completions:
            def create(self, **kw):
                outer.chat_calls.append(kw)
                return type("R", (), {"model": kw.get("model"), "id": "cmpl-1"})()

        self.models = _Models()
        self.chat = type("C", (), {"completions": _Completions()})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_openai(monkeypatch):
    _FakeOpenAI.instances = []
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    return _FakeOpenAI


def test_validate_anthropic_probes_models_list_not_a_model_id(monkeypatch):
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    result = validators.validate_anthropic("sk-ant-good")
    assert result.ok is True, result.error
    assert result.detail["model_count"] == 3
    assert _FakeAnthropic.seen["api_key"] == "sk-ant-good"


def test_validate_anthropic_rejected_key_is_invalid(monkeypatch):
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    result = validators.validate_anthropic("sk-ant-bad")
    assert result.ok is False
    assert "401" in result.error


def test_validate_xai_probes_models_list_on_xai(fake_openai):
    result = validators.validate_xai("xai-good")
    assert result.ok is True, result.error
    assert result.detail["model_count"] == 2
    client = fake_openai.instances[-1]
    assert client.base_url == "https://api.x.ai/v1"
    assert client.chat_calls == [], "the xAI probe must not spend tokens on a named model"


def test_validate_xai_rejected_key_is_invalid(fake_openai):
    result = validators.validate_xai("bad")
    assert result.ok is False
    assert "401" in result.error


def test_validate_perplexity_probes_the_configured_search_model(fake_openai, monkeypatch):
    from Orchestrator import config
    monkeypatch.setattr(config, "PERPLEXITY_SEARCH_MODEL", "sonar-next", raising=False)
    result = validators.validate_perplexity("pplx-good")
    assert result.ok is True, result.error
    client = fake_openai.instances[-1]
    assert client.base_url == "https://api.perplexity.ai"
    assert [c["model"] for c in client.chat_calls] == ["sonar-next"]
    assert client.chat_calls[0]["max_tokens"] == 16  # Perplexity's minimum


def _ini(text=""):
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_perplexity_search_model_defaults_to_sonar():
    """Resolution over explicit inputs, so a box or CI that sets the knob does
    not fail this test."""
    from Orchestrator.config import resolve_perplexity_search_model
    assert resolve_perplexity_search_model({}, _ini()) == "sonar"
    assert resolve_perplexity_search_model({"PERPLEXITY_SEARCH_MODEL": "  "}, _ini()) == "sonar"


def test_perplexity_search_model_env_wins_then_config_ini():
    from Orchestrator.config import resolve_perplexity_search_model
    ini = _ini("[web_search]\nperplexity_model = sonar-ini\n")
    assert resolve_perplexity_search_model({}, ini) == "sonar-ini"
    assert resolve_perplexity_search_model({"PERPLEXITY_SEARCH_MODEL": "sonar-env"}, ini) == "sonar-env"


def test_web_search_and_key_probe_share_the_perplexity_model(monkeypatch):
    """One retirement fix covers both sites: the search adapter sends the same
    config value the validator probes with."""
    import Orchestrator.web_tools as wt
    sent = {}

    def _fake_post(url, headers=None, json=None, timeout=None, **kw):
        sent.update(json or {})
        return _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(wt, "PERPLEXITY_API_KEY", "pplx-good")
    monkeypatch.setattr(wt, "PERPLEXITY_SEARCH_MODEL", "sonar-next", raising=False)
    monkeypatch.setattr(wt.requests, "post", _fake_post)
    wt._search_perplexity("q", "month")
    assert sent["model"] == "sonar-next"


def test_validate_voyage_probes_the_rerank_catalog_model(monkeypatch):
    from Orchestrator import rerank
    entry = dict(rerank.RERANK_MODELS["voyage-rerank-2.5"], model_id="rerank-9")
    monkeypatch.setitem(rerank.RERANK_MODELS, "voyage-rerank-2.5", entry)
    monkeypatch.setattr(rerank, "get_settings", lambda: {"provider": "null", "model_id": "x"})
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["json"] = json
        return _FakeResponse(200, {"model": json["model"], "data": []})

    monkeypatch.setattr("requests.post", _fake_post)
    result = validators.validate_voyage("pa-good")
    assert result.ok is True, result.error
    assert captured["json"]["model"] == "rerank-9"
    assert result.detail["model"] == "rerank-9"


def test_validate_voyage_probes_the_selected_voyage_model(monkeypatch):
    """A box pinned to a Voyage reranker is probed with the model it scores with."""
    from Orchestrator import rerank
    monkeypatch.setattr(rerank, "get_settings",
                        lambda: {"provider": "voyage", "model_id": "rerank-2.5-lite"})
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["json"] = json
        return _FakeResponse(200, {"data": []})

    monkeypatch.setattr("requests.post", _fake_post)
    result = validators.validate_voyage("pa-good")
    assert result.ok is True, result.error
    assert captured["json"]["model"] == "rerank-2.5-lite"


def test_validators_source_names_no_probe_model_literal():
    """Regression guard: no `model=` keyword or "model" dict key in validators.py
    may carry a string literal -- every probe id is discovered or configured."""
    import ast
    from pathlib import Path
    src = Path(validators.__file__).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.keyword) and node.arg == "model" \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            offenders.append((node.value.lineno, node.value.value))
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "model" \
                        and isinstance(v, ast.Constant) and isinstance(v.value, str):
                    offenders.append((v.lineno, v.value))
    assert offenders == []
