"""Reranker provider seam (Orchestrator/rerank.py) + /rerank/status (M11/WI-4).

All fixture/fake based — NO live model serving exists yet (the GPU is not
installed): these tests pin the provider abstraction contract (score() never
raises, None on any failure), the one-time latency preflight semantics
(failure disables for the process lifetime), the RERANK_MODELS registry
conventions, and the /rerank/status payload shape (ADDITIVE ops contract).
"""
import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import rerank
from Orchestrator.config import CFG
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.routes.rerank_routes import router


# ── config pinning (in-process CFG only; disk untouched) ─────────────────────

@contextlib.contextmanager
def pin_cfg(section: str, **keys):
    """Pin options on the in-process CFG (value None = ensure ABSENT);
    restore exactly afterwards."""
    if not CFG.has_section(section):
        CFG.add_section(section)
    saved = {
        opt: (CFG.get(section, opt) if CFG.has_option(section, opt) else None)
        for opt in keys
    }
    try:
        for opt, val in keys.items():
            if val is None:
                CFG.remove_option(section, opt)
            else:
                CFG.set(section, opt, str(val))
        yield
    finally:
        for opt, prev in saved.items():
            if prev is None:
                CFG.remove_option(section, opt)
            else:
                CFG.set(section, opt, prev)


@pytest.fixture(autouse=True)
def _clean_preflight():
    """The preflight + reachability caches are process state — isolate every
    test (reset_preflight clears both)."""
    rerank.reset_preflight()
    yield
    rerank.reset_preflight()


@pytest.fixture(autouse=True)
def _no_network_reach(monkeypatch):
    """service_reachable (M13) must NEVER hit the real network in tests —
    base_url now falls back to http://localhost:8091, and a dev box may have
    a live vLLM there. Default = connection refused; tests that want a
    reachable service re-monkeypatch rerank.requests.get themselves."""
    def refuse(*a, **k):
        raise ConnectionError("test: network disabled")
    monkeypatch.setattr(rerank.requests, "get", refuse)


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


# ── RERANK_MODELS registry conventions ────────────────────────────────────────

def test_rerank_models_table_shape():
    assert set(rerank.RERANK_MODELS) == {"qwen3-reranker-0.6b", "qwen3-reranker-4b"}
    for slug, entry in rerank.RERANK_MODELS.items():
        assert slug == slug.lower() and "_" not in slug, "slugs are kebab-case"
        for field in ("provider", "model_id", "label", "vram_gb",
                      "max_input_tokens", "quality_note"):
            assert field in entry, f"{slug} missing {field}"
        assert entry["provider"] == "vllm"
        assert isinstance(entry["vram_gb"], float)


def test_rerank_models_never_pollute_embedding_registry():
    """Rerankers are NOT embedding models (module-docstring discipline):
    the two registries must stay slug-disjoint."""
    assert not set(rerank.RERANK_MODELS) & set(EMBEDDING_MODELS)


def test_settings_resolve_slug_to_model_id_and_pass_unknown_verbatim():
    with pin_cfg("rerank", provider="vllm", base_url="http://x:1",
                 model="qwen3-reranker-4b"):
        assert rerank.get_settings()["model_id"] == "Qwen/Qwen3-Reranker-4B"
    with pin_cfg("rerank", provider="vllm", base_url="http://x:1",
                 model="my/custom-served-name"):
        assert rerank.get_settings()["model_id"] == "my/custom-served-name"


def test_settings_fresh_box_defaults_are_inert():
    """[rerank] section absent (audit A13 fresh-box rule): null provider =
    inert, default slug, 500ms ceiling. base_url falls back to the installer's
    vllm-reranker.service port (M13) so a fresh GPU box needs zero url/port
    config edits — provider stays the single deliberate switch."""
    with pin_cfg("rerank", provider=None, base_url=None, model=None,
                 timeout_s=None, preflight_ceiling_ms=None):
        s = rerank.get_settings()
    assert s["provider"] == "null"
    assert s["base_url"] == "http://localhost:8091"
    assert s["base_url"] == rerank.DEFAULT_BASE_URL
    assert s["model"] == "qwen3-reranker-0.6b"
    assert s["preflight_ceiling_ms"] == 500.0


# ── score(): provider dispatch + vLLM /score parsing, never raises ───────────

def test_score_null_provider_returns_none():
    with pin_cfg("rerank", provider="null", base_url=None):
        assert rerank.score("q", ["p1", "p2"]) is None


def test_score_vllm_with_explicitly_empty_base_url_returns_none():
    """An EXPLICITLY EMPTY base_url is the disable escape hatch (M13) — only
    an ABSENT key falls back to the installer's default URL."""
    with pin_cfg("rerank", provider="vllm", base_url=""):
        assert rerank.score("q", ["p"]) is None


def test_score_vllm_absent_base_url_falls_back_to_installer_port(monkeypatch):
    """Fresh-box M13 contract: [rerank] base_url absent → the wire call goes
    to the installer's vllm-reranker.service (http://localhost:8091)."""
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        return FakeResp(200, {"data": [{"index": 0, "score": 0.5}]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="vllm", base_url=None):
        assert rerank.score("q", ["p"]) == [0.5]
    assert calls["url"] == "http://localhost:8091/score"


def test_score_vllm_parses_and_honors_index_alignment(monkeypatch):
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"], calls["json"], calls["timeout"] = url, json, timeout
        # out-of-order rows: index must map scores back to passage positions.
        return FakeResp(200, {"data": [
            {"index": 1, "score": 0.9}, {"index": 0, "score": 0.1},
        ]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:8091/",
                 model="qwen3-reranker-0.6b", timeout_s="7"):
        got = rerank.score("the query", ["pass A", "pass B"])
    assert got == [0.1, 0.9]
    assert calls["url"] == "http://h:8091/score"      # trailing / stripped
    # text_1 carries the Qwen3-Reranker instruct prefix (required for correct
    # scoring — see RERANK_MODELS comment); the raw query follows it.
    assert calls["json"]["model"] == "Qwen/Qwen3-Reranker-0.6B"
    assert calls["json"]["text_1"].endswith("\nQuery: the query")
    assert calls["json"]["text_1"].startswith("Instruct:")
    assert calls["json"]["text_2"] == ["pass A", "pass B"]
    assert calls["timeout"] == 7.0


def test_score_prepends_model_query_instruction(monkeypatch):
    """Qwen3-Reranker inverts without its instruct prefix (measured on GPU);
    score() must prepend the active model's query_instruction to the query."""
    seen = {}
    monkeypatch.setattr(rerank.requests, "post",
                        lambda url, json=None, timeout=None:
                        (seen.update(json), FakeResp(200, {"data": [{"index": 0, "score": 0.5}]}))[1])
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 model="qwen3-reranker-0.6b"):
        rerank.score("fix the truncation", ["some passage"])
    assert seen["text_1"] == (
        "Instruct: Given a search query, retrieve relevant passages that answer the query"
        "\nQuery: fix the truncation")


def test_score_config_query_instruction_override(monkeypatch):
    """[rerank] query_instruction overrides the model default; empty = bare query."""
    seen = {}
    monkeypatch.setattr(rerank.requests, "post",
                        lambda url, json=None, timeout=None:
                        (seen.update(json), FakeResp(200, {"data": [{"index": 0, "score": 0.5}]}))[1])
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 model="qwen3-reranker-0.6b", query_instruction=""):
        rerank.score("bare", ["p"])
    assert seen["text_1"] == "bare"


def test_score_vllm_missing_index_falls_back_to_order(monkeypatch):
    monkeypatch.setattr(rerank.requests, "post", lambda *a, **k: FakeResp(
        200, {"data": [{"score": 0.3}, {"score": 0.8}]}))
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", ["a", "b"]) == [0.3, 0.8]


@pytest.mark.parametrize("resp", [
    FakeResp(500, {}),                                     # HTTP error
    FakeResp(200, {"data": [{"index": 0, "score": 1.0}]}),  # wrong row count
    FakeResp(200, {"data": "garbage"}),                    # malformed
    FakeResp(200, {}),                                     # no data key
])
def test_score_vllm_bad_response_returns_none(monkeypatch, resp):
    monkeypatch.setattr(rerank.requests, "post", lambda *a, **k: resp)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", ["a", "b"]) is None


def test_score_vllm_transport_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", ["a"]) is None  # never raises (audit A9)


# ── one-time latency preflight ────────────────────────────────────────────────

def test_preflight_unconfigured_is_skipped_and_not_cached(monkeypatch):
    with pin_cfg("rerank", provider="null", base_url=None):
        pf = rerank.preflight()
    assert pf["state"] == "skipped"
    assert rerank.available() is False
    # NOT cached: configuring a provider afterwards must still probe.
    monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="5000"):
        assert rerank.preflight()["state"] == "ok"


def test_preflight_ok_caches_and_enables(monkeypatch):
    monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="5000"):
        assert rerank.preflight()["state"] == "ok"
        assert rerank.available() is True
        # cached: a now-broken scorer must NOT be re-probed this process.
        def boom(q, p):
            raise AssertionError("preflight re-probed despite cache")
        monkeypatch.setattr(rerank, "score", boom)
        assert rerank.preflight()["state"] == "ok"
        assert rerank.available() is True


def test_preflight_scoring_failure_disables_for_process(monkeypatch):
    monkeypatch.setattr(rerank, "score", lambda q, p: None)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="5000"):
        pf = rerank.preflight()
        assert pf["state"] == "failed"
        assert pf["reason"] == "provider scoring failed"
        assert rerank.available() is False
        # process-lifetime: a now-healthy scorer is NOT consulted again.
        monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
        assert rerank.available() is False


def test_preflight_over_ceiling_disables_for_process(monkeypatch, capsys):
    monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
    # ceiling -1ms: any successful probe is over-ceiling -> latency failure.
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="-1"):
        pf = rerank.preflight()
        assert pf["state"] == "failed"
        assert "over the" in pf["reason"]
        assert pf["latency_ms"] is not None
        assert rerank.available() is False
    out = capsys.readouterr().out
    assert "[RERANK] preflight failed" in out
    assert "disabled for process lifetime" in out


# ── GET /rerank/status ────────────────────────────────────────────────────────

STATUS_KEYS = {
    "enabled", "provider", "model", "model_id", "base_url", "configured",
    "preflight", "available", "candidate_n", "passage_chars", "models",
    # M13 additive keys for the wizard's reranker block:
    "gpu", "service_reachable",
}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _pin_hardware(monkeypatch, gpu: bool):
    """Deterministic host-hardware probe (the dev box HAS the GPU, CI hasn't)."""
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": gpu, "gpu_name": "RTX Test" if gpu else None,
        "vram_mb": 16380 if gpu else None, "ram_mb": 32768,
        "source": "nvidia-smi" if gpu else "none",
    })


def test_status_shape_on_the_pre_gpu_default_box(client, monkeypatch):
    """Null provider + flag off on a CPU box — the fresh-box default. No
    latency preflight runs; the only probe is the ~1s-capped reachability
    check (stubbed refused here)."""
    _pin_hardware(monkeypatch, gpu=False)
    with pin_cfg("rerank", provider="null", base_url=None), \
         pin_cfg("retrieval", rerank_enabled=None, rerank_candidate_n=None):
        r = client.get("/rerank/status")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == STATUS_KEYS
    assert body["enabled"] is False
    assert body["provider"] == "null"
    assert body["configured"] is False
    assert body["available"] is False
    assert body["preflight"]["state"] == "skipped"
    assert body["candidate_n"] == 40
    assert body["models"] == sorted(rerank.RERANK_MODELS)
    # M13: CPU box, nothing listening → the wizard's one muted line state.
    assert body["gpu"] is False
    assert body["service_reachable"] is False
    # base_url resolves to the installer's default even when unconfigured.
    assert body["base_url"] == rerank.DEFAULT_BASE_URL


def test_status_fresh_gpu_box_with_service_up_awaits_the_config_flip(
        client, monkeypatch):
    """M13 zero-config contract: [rerank] absent + installer's service up →
    gpu + service_reachable true while configured/enabled stay false — the
    wizard renders the enable instruction (config flip + restart), nothing
    auto-enables."""
    _pin_hardware(monkeypatch, gpu=True)
    monkeypatch.setattr(rerank.requests, "get",
                        lambda url, timeout=None: FakeResp(200, {"data": []}))
    with pin_cfg("rerank", provider=None, base_url=None), \
         pin_cfg("retrieval", rerank_enabled=None):
        body = client.get("/rerank/status").json()
    assert body["gpu"] is True
    assert body["service_reachable"] is True
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["available"] is False


def test_service_reachable_probes_v1_models_and_caches(monkeypatch):
    """One GET per TTL window; reset_preflight clears the cache too."""
    seen = []

    def fake_get(url, timeout=None):
        seen.append((url, timeout))
        return FakeResp(200, {})

    monkeypatch.setattr(rerank.requests, "get", fake_get)
    with pin_cfg("rerank", provider=None, base_url=None):
        assert rerank.service_reachable() is True
        assert rerank.service_reachable() is True  # cached — no second GET
        assert len(seen) == 1
        assert seen[0][0] == "http://localhost:8091/v1/models"
        assert seen[0][1] == 1.0  # ~1s cap: never blocks the wizard
        rerank.reset_preflight()
        assert rerank.service_reachable() is True
        assert len(seen) == 2


def test_service_reachable_false_on_refused_or_explicit_empty(monkeypatch):
    # Autouse stub refuses connections → False (and cached as False).
    with pin_cfg("rerank", provider=None, base_url=None):
        assert rerank.service_reachable() is False
    rerank.reset_preflight()
    # Explicitly empty base_url: nothing to probe, never a network call.
    def boom(*a, **k):
        raise AssertionError("probed despite empty base_url")
    monkeypatch.setattr(rerank.requests, "get", boom)
    with pin_cfg("rerank", provider="vllm", base_url=""):
        assert rerank.service_reachable() is False


def test_status_reports_failed_preflight(client, monkeypatch):
    monkeypatch.setattr(rerank, "score", lambda q, p: None)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"), \
         pin_cfg("retrieval", rerank_enabled="true"):
        body = client.get("/rerank/status").json()
    assert body["enabled"] is True
    assert body["configured"] is True
    assert body["preflight"]["state"] == "failed"
    assert body["available"] is False


def test_status_reports_ok_preflight_and_available(client, monkeypatch):
    monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="5000", model="qwen3-reranker-0.6b"), \
         pin_cfg("retrieval", rerank_enabled="true"):
        body = client.get("/rerank/status").json()
    assert body["preflight"]["state"] == "ok"
    assert body["available"] is True
    assert body["model_id"] == "Qwen/Qwen3-Reranker-0.6B"
