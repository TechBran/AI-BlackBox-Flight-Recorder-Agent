"""Reranker keys are read .env-first, os.environ fallback (audit
O16-onboarding-update-04).

The wizard's API Keys step saves VOYAGE_API_KEY / COHERE_API_KEY through
POST /onboarding/save, which writes .env and never os.environ. rerank used to
read os.getenv only, so a freshly pasted key showed as missing (the Memory
step's reranker card stayed unselectable) until an unrelated restart, and
/restart-status never asked for one. Now every key read goes through
providers.read_env_value, the same .env-first order elevenlabs uses.

Hermetic: secrets_writer.ENV_FILE points at a tmp file; requests.post is faked.
"""
from __future__ import annotations

import pytest

from Orchestrator import rerank
from Orchestrator.onboarding import secrets_writer


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(secrets_writer, "ENV_FILE", path)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    return path


def test_key_saved_only_to_dotenv_is_present(env_file):
    assert rerank._key_present_for("bearer_env", "VOYAGE_API_KEY") is False
    env_file.write_text("VOYAGE_API_KEY=pa-fresh-paste\n")
    assert rerank._key_present_for("bearer_env", "VOYAGE_API_KEY") is True


def test_model_catalog_sees_a_dotenv_only_key(env_file):
    def _voyage():
        return [m for m in rerank.model_catalog() if m["provider"] == "voyage"]
    assert _voyage() and not any(m["key_present"] for m in _voyage())
    env_file.write_text("VOYAGE_API_KEY=pa-fresh-paste\n")
    assert all(m["key_present"] for m in _voyage())


def test_process_env_is_still_the_fallback(env_file, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "co-process")
    assert rerank._key_present_for("bearer_env", "COHERE_API_KEY") is True


class _Resp:
    status_code = 200

    def __init__(self, n):
        self._n = n

    def json(self):
        return {"data": [{"index": i, "relevance_score": 1.0 - i / 10}
                         for i in range(self._n)],
                "results": [{"index": i, "relevance_score": 1.0 - i / 10}
                            for i in range(self._n)]}


@pytest.mark.parametrize("scorer,key_env", [
    (rerank._score_voyage, "VOYAGE_API_KEY"),
    (rerank._score_cohere, "COHERE_API_KEY"),
])
def test_scorers_send_the_dotenv_key_over_a_stale_process_value(
        env_file, monkeypatch, scorer, key_env):
    """.env is the file the wizard just wrote: it wins over a stale os.environ
    value left from boot."""
    env_file.write_text(f"{key_env}=fresh-from-dotenv\n")
    monkeypatch.setenv(key_env, "stale-from-boot")
    sent = {}

    def _post(url, headers=None, json=None, timeout=None):
        sent["auth"] = headers["Authorization"]
        return _Resp(len(json.get("documents") or []))
    monkeypatch.setattr(rerank.requests, "post", _post)
    settings = {"key_env": key_env, "model_id": "m", "timeout_s": 1.0}
    assert scorer("q", ["a", "b"], settings) is not None
    assert sent["auth"] == "Bearer fresh-from-dotenv"


def test_llm_scorer_reads_the_dotenv_key(env_file, monkeypatch):
    env_file.write_text("XAI_API_KEY=xai-from-dotenv\n")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    seen = {}

    def _complete(model_id, api_key, prompt, timeout_s):
        seen["key"] = api_key
        return "[0, 1]"
    monkeypatch.setitem(rerank._LLM_COMPLETERS, "XAI_API_KEY", _complete)
    out = rerank._score_llm("q", ["a", "b"],
                            {"key_env": "XAI_API_KEY", "model_id": "m", "timeout_s": 1.0})
    assert out is not None
    assert seen["key"] == "xai-from-dotenv"
