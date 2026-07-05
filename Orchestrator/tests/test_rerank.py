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


@pytest.fixture(autouse=True)
def _isolate_rerank_sidecar(tmp_path, monkeypatch):
    """M4: get_settings()/is_enabled() now read the rerank.json sidecar under
    config.EMBEDDINGS_STORES_DIR. Point every test at an EMPTY tmp stores dir so
    no test accidentally reads a real selection (and the sidecar-absent →
    config-fallback contract holds for the pre-M4 tests). Tests that WANT a
    sidecar write one via _write_sidecar()."""
    from Orchestrator import config as _config
    monkeypatch.setattr(_config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "stores"))


def _write_sidecar(selection: dict) -> dict:
    """Write a rerank.json into the (monkeypatched) tmp stores dir."""
    from Orchestrator.embeddings import store as _store
    return _store.set_rerank_selection(selection)


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


# ── M3 fakes: registry entries + a controllable clock ────────────────────────
# M7/M5 cloud+cpu RERANK_MODELS entries don't exist yet, so M3 tests inject
# temporary entries via monkeypatch.setitem to exercise per-provider ceilings.

def _fake_cloud_entry(provider="voyage", key_env="VOYAGE_API_KEY",
                      ceiling=1200, passage_n=1):
    return {
        "provider": provider, "model_id": f"{provider}-rerank",
        "label": "Fake cloud reranker", "vram_gb": 0.0,
        "max_input_tokens": 8192, "quality_note": "test-only",
        "auth_kind": "bearer_env", "key_env": key_env, "cost_note": "test",
        "privacy": "cloud", "tiers": ["LOW", "MID", "HIGH"],
        "preflight_ceiling_ms": ceiling, "preflight_passage_n": passage_n,
    }


def _fake_cpu_entry(ceiling=2000, passage_n=8):
    return {
        "provider": "cpu", "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "label": "Fake CPU reranker", "vram_gb": 0.0,
        "max_input_tokens": 32768, "quality_note": "test-only",
        "auth_kind": "none", "key_env": None, "cost_note": "test",
        "privacy": "local", "tiers": ["MID"],
        "preflight_ceiling_ms": ceiling, "preflight_passage_n": passage_n,
        "query_instruction": "Instruct: rank\nQuery: ",
    }


class Clock:
    """Deterministic monotonic() stand-in; advance by mutating .t."""
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def make_scorer(clock, elapse_s, result):
    """A fake rerank.score that 'takes' elapse_s of clock time and records the
    passage count it was probed with."""
    def _score(query, passages):
        _score.passages = list(passages)
        clock.t += elapse_s
        return result
    _score.passages = None
    return _score


# ── RERANK_MODELS registry conventions ────────────────────────────────────────

# M2 Task 2.1 schema enums (mirror the embeddings registry guard style).
RERANK_AUTH_KINDS = {"none", "bearer_env", "gcp_service_account", "frontier_key"}
RERANK_PRIVACY = {"local", "cloud"}
RERANK_TIERS = {"LOW", "MID", "HIGH"}


def test_rerank_models_table_shape():
    assert set(rerank.RERANK_MODELS) == {
        "qwen3-reranker-0.6b", "qwen3-reranker-4b", "qwen3-reranker-0.6b-cpu",
        "llm-rerank-gemini-flash", "llm-rerank-gpt-mini",
        "llm-rerank-claude-haiku", "llm-rerank-grok",
        "voyage-rerank-2.5", "cohere-rerank-4", "vertex-semantic-ranker"}
    for slug, entry in rerank.RERANK_MODELS.items():
        assert slug == slug.lower() and "_" not in slug, "slugs are kebab-case"
        for field in ("provider", "model_id", "label",
                      "max_input_tokens", "quality_note"):
            assert field in entry, f"{slug} missing {field}"
        # Local footprint field (M10 wizard display): GPU (vllm) entries carry
        # vram_gb, the in-process CPU entry ram_gb — both floats. Cloud providers
        # (llm now; voyage/cohere/vertex in M7) have no local footprint.
        if entry["provider"] == "cpu":
            assert isinstance(entry["ram_gb"], float), f"{slug}: ram_gb float"
        elif entry["provider"] == "vllm":
            assert isinstance(entry["vram_gb"], float), f"{slug}: vram_gb float"


@pytest.mark.parametrize("slug", list(rerank.RERANK_MODELS))
def test_every_rerank_model_declares_new_schema_keys(slug):
    """M2 Task 2.1: every RERANK_MODELS entry declares the tiering-schema keys
    with valid types + enum membership (mirrors test_embeddings_registry.py's
    per-entry guard). auth_kind/key_env/cost_note/privacy/tiers plus the
    per-provider preflight_ceiling_ms/preflight_passage_n."""
    e = rerank.RERANK_MODELS[slug]
    assert e["auth_kind"] in RERANK_AUTH_KINDS, f"{slug}: bad auth_kind"
    assert e["key_env"] is None or (
        isinstance(e["key_env"], str) and e["key_env"]
    ), f"{slug}: key_env must be a non-empty str or None"
    assert isinstance(e["cost_note"], str) and e["cost_note"], f"{slug}: cost_note"
    assert e["privacy"] in RERANK_PRIVACY, f"{slug}: bad privacy"
    assert isinstance(e["tiers"], list) and e["tiers"], f"{slug}: tiers non-empty list"
    assert set(e["tiers"]) <= RERANK_TIERS, f"{slug}: tiers must subset LOW/MID/HIGH"
    for k in ("preflight_ceiling_ms", "preflight_passage_n"):
        v = e[k]
        assert isinstance(v, int) and not isinstance(v, bool) and v > 0, (
            f"{slug}: {k} must be a positive int"
        )


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


# ── score(): M2 provider dispatcher ───────────────────────────────────────────

def test_score_dispatches_by_provider(monkeypatch):
    """M2 Task 2.2: score() routes to the helper named by settings['provider'],
    passing exactly (query, passages, settings). Proven for the vLLM path AND a
    cloud path so the dispatch is provider-keyed, not vLLM-hardcoded."""
    seen = {}

    def fake_vllm(query, passages, settings):
        seen["called"] = "vllm"
        seen["args"] = (query, passages, settings)
        return [0.42]

    monkeypatch.setattr(rerank, "_score_vllm", fake_vllm)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", ["p"]) == [0.42]
    assert seen["called"] == "vllm"
    q, passages, settings = seen["args"]
    assert q == "q" and passages == ["p"]
    assert isinstance(settings, dict) and settings["provider"] == "vllm"

    # A different provider routes to its own helper (no key plumbing yet — M4).
    def fake_voyage(query, passages, settings):
        seen["called"] = "voyage"
        return [0.9]

    monkeypatch.setattr(rerank, "_score_voyage", fake_voyage)
    with pin_cfg("rerank", provider="voyage"):
        assert rerank.score("q", ["p"]) == [0.9]
    assert seen["called"] == "voyage"


def test_unknown_provider_returns_none():
    with pin_cfg("rerank", provider="banana"):
        assert rerank.score("q", ["p"]) is None


def test_null_provider_returns_none():
    with pin_cfg("rerank", provider="null", base_url=None):
        assert rerank.score("q", ["p1", "p2"]) is None


def test_empty_passages_returns_none():
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", []) is None


def test_dispatcher_never_raises(monkeypatch):
    """A provider helper that raises must be swallowed by the dispatcher's
    never-raise backstop (audit A9): score() returns None, not an exception."""
    def boom(query, passages, settings):
        raise RuntimeError("provider blew up")
    monkeypatch.setattr(rerank, "_score_vllm", boom)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.score("q", ["p"]) is None


def test_known_providers_set():
    assert rerank.KNOWN_PROVIDERS == {
        "null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm"}


# ── M5: in-process CPU CrossEncoder provider ──────────────────────────────────
# The real sentence-transformers/torch stack is NOT installed here (LOW box +
# CI) — _score_cpu lazy-imports it, so we inject a fake `sentence_transformers`
# module exposing CrossEncoder via sys.modules. This is the seam that proves the
# lazy `from sentence_transformers import CrossEncoder` resolves without the
# real dep. The CPU model cache is process state cleared by reset_preflight()
# (the autouse _clean_preflight fixture isolates every test).

def _install_fake_cross_encoder(monkeypatch, predict_impl=None):
    """Inject a fake sentence_transformers module; return the FakeCrossEncoder
    class whose .constructed (model_ids passed to __init__) and .last_pairs
    record calls. predict_impl(pairs) -> scores; default returns 0.5 per pair."""
    import sys
    import types

    class FakeCrossEncoder:
        constructed: list = []
        last_pairs = None

        def __init__(self, model_id, *a, **k):
            FakeCrossEncoder.constructed.append(model_id)

        def predict(self, pairs, *a, **k):
            FakeCrossEncoder.last_pairs = list(pairs)
            if predict_impl is not None:
                return predict_impl(FakeCrossEncoder.last_pairs)
            return [0.5] * len(FakeCrossEncoder.last_pairs)

    mod = types.ModuleType("sentence_transformers")
    mod.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    return FakeCrossEncoder


def test_score_cpu_returns_aligned_floats(monkeypatch):
    """A fake CrossEncoder's per-pair scores come back as python floats aligned
    to `passages` (the real lib returns an ndarray → numpy→float conversion)."""
    fake = _install_fake_cross_encoder(
        monkeypatch, predict_impl=lambda pairs: [0.1, 0.9, 0.5])
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        got = rerank.score("q", ["p1", "p2", "p3"])
    assert got == [0.1, 0.9, 0.5]
    assert all(isinstance(v, float) for v in got)
    assert len(fake.last_pairs) == 3


def test_score_cpu_import_failure_returns_none(monkeypatch):
    """No sentence_transformers installed → None, never raises (absent deps →
    inert; a fresh LOW box has no torch)."""
    import sys
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        assert rerank.score("q", ["p1", "p2"]) is None


def test_score_cpu_prepends_query_instruction(monkeypatch):
    """Parity with _score_vllm: the Qwen instruct prefix is prepended to the
    query before scoring (the CPU CrossEncoder is the SAME model — it inverts
    without its instruct prefix)."""
    fake = _install_fake_cross_encoder(monkeypatch)
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        rerank.score("fix truncation", ["some passage"])
    instructed_query, passage = fake.last_pairs[0]
    assert instructed_query == (
        "Instruct: Given a search query, retrieve relevant passages that answer the query"
        "\nQuery: fix truncation")
    assert passage == "some passage"


def test_score_cpu_model_cached(monkeypatch):
    """The loaded CrossEncoder is process-cached (keyed by model_id): two score
    calls construct it exactly once (mirrors the preflight cache pattern)."""
    fake = _install_fake_cross_encoder(monkeypatch)
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        rerank.score("q1", ["p"])
        rerank.score("q2", ["p"])
    assert fake.constructed == ["Qwen/Qwen3-Reranker-0.6B"]


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


# ── M3.1 per-provider ceilings + realistic passage-count probe ────────────────

def test_cloud_ceiling_from_registry(monkeypatch):
    """A cloud model's ceiling comes from its RERANK_MODELS entry: a ~600ms
    probe PASSES the 1200ms registry ceiling though it would fail the old
    hardcoded 500ms."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(ceiling=1200, passage_n=1))
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    monkeypatch.setattr(rerank, "score", make_scorer(clock, 0.6, [1.0]))
    with pin_cfg("rerank", provider="voyage", model="fake-voyage",
                 preflight_ceiling_ms=None):
        pf = rerank.preflight()
    assert pf["ceiling_ms"] == 1200.0        # from the registry, not 500
    assert pf["latency_ms"] == 600.0
    assert pf["state"] == "ok"               # 600 < 1200 (would fail old 500)


def test_cpu_probe_extrapolates_to_candidate_n(monkeypatch):
    """The CPU probe scores preflight_passage_n passages (not 1) and
    extrapolates measured_ms × candidate_n / passage_n before the compare."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-cpu",
                        _fake_cpu_entry(ceiling=2000, passage_n=8))
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    scorer = make_scorer(clock, 0.3, [1.0] * 8)   # 300ms for the 8-passage probe
    monkeypatch.setattr(rerank, "score", scorer)
    with pin_cfg("rerank", provider="cpu", model="fake-cpu",
                 base_url="http://x:1", preflight_ceiling_ms=None), \
         pin_cfg("retrieval", rerank_candidate_n="40"):
        pf = rerank.preflight()
    assert len(scorer.passages) == 8         # probed 8, not 1
    assert pf["measured_ms"] == 300.0
    assert pf["latency_ms"] == 1500.0        # 300 × (40/8)
    assert pf["ceiling_ms"] == 2000.0
    assert pf["state"] == "ok"               # 1500 < 2000


def test_config_ceiling_override_wins(monkeypatch):
    """[rerank] preflight_ceiling_ms still overrides the registry ceiling."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(ceiling=1200, passage_n=1))
    with pin_cfg("rerank", provider="voyage", model="fake-voyage",
                 preflight_ceiling_ms="999"):
        assert rerank.get_settings()["preflight_ceiling_ms"] == 999.0


def test_get_settings_survives_malformed_numeric_config(monkeypatch):
    """A hand-edited non-numeric [rerank] value falls back to the default
    rather than raising — restores score()/status()'s never-raise contract."""
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": False, "gpu_name": None, "vram_mb": None, "ram_mb": 32768,
        "source": "none", "tier": "MID"})
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 timeout_s="not-a-number", preflight_ceiling_ms="also-bad"):
        s = rerank.get_settings()
        assert s["timeout_s"] == 15.0                 # fallback, not a raise
        assert s["preflight_ceiling_ms"] == 500.0     # fallback, not a raise
        assert rerank.score("q", ["p"]) is None       # never raises
        assert isinstance(rerank.status(), dict)      # never raises


# ── M3.2 TTL-recoverable cloud preflight + provider-aware reachability ─────────

def test_cloud_failed_preflight_recovers_after_ttl(monkeypatch):
    """A cloud provider's FAILED preflight is cached only for _PREFLIGHT_FAIL_TTL_S
    (a transient blip must not disable rerank until restart) — unlike a local
    provider's process-lifetime failure."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(ceiling=5000, passage_n=1))
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    monkeypatch.setattr(rerank, "score", lambda q, p: None)      # cloud down
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"):
        assert rerank.preflight()["state"] == "failed"
        # within the TTL window the failure is cached — NOT re-probed
        def reprobed(q, p):
            raise AssertionError("cloud preflight re-probed within its TTL")
        monkeypatch.setattr(rerank, "score", reprobed)
        assert rerank.preflight()["state"] == "failed"
        # past the TTL, cloud recovered → re-probe → ok
        clock.t += rerank._PREFLIGHT_FAIL_TTL_S + 1
        monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
        assert rerank.preflight()["state"] == "ok"


def test_local_failed_preflight_stays_process_lifetime(monkeypatch):
    """The TTL is CLOUD-only: a vllm failure still sticks for the process
    lifetime even after the same elapsed time (deterministic local provider)."""
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    monkeypatch.setattr(rerank, "score", lambda q, p: None)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="5000"):
        assert rerank.preflight()["state"] == "failed"
        clock.t += rerank._PREFLIGHT_FAIL_TTL_S + 100
        monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])  # would pass
        assert rerank.preflight()["state"] == "failed"            # still sticky


def test_reachable_bearer_is_key_present_no_network(monkeypatch):
    """reachable() for a bearer cloud provider is a pure key-present check —
    ZERO http calls (actual cloud reachability is proven once by preflight)."""
    def no_net(*a, **k):
        raise AssertionError("cloud reachability must not hit the network")
    monkeypatch.setattr(rerank.requests, "get", no_net)
    monkeypatch.setattr(rerank.requests, "post", no_net)
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(key_env="VOYAGE_API_KEY"))
    monkeypatch.setenv("VOYAGE_API_KEY", "present")
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"):
        assert rerank.reachable() is True
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"):
        assert rerank.reachable() is False


def test_reachable_vertex_uses_sa_creds_path(monkeypatch):
    """M8 fold-in (M7 review nit #1): Vertex (gcp_service_account, key_env None)
    reports reachable from the ambient SA creds path
    (GOOGLE_APPLICATION_CREDENTIALS the credentials route sets), NOT the
    always-False key_present. Pure env read — ZERO network."""
    def no_net(*a, **k):
        raise AssertionError("vertex reachability must not hit the network")
    monkeypatch.setattr(rerank.requests, "get", no_net)
    monkeypatch.setattr(rerank.requests, "post", no_net)
    with pin_cfg("rerank", provider="vertex", model="vertex-semantic-ranker"):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
        assert rerank.reachable() is True
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        assert rerank.reachable() is False


def test_scatter_relevance_scores_gap_returns_none():
    """M8 fold-in (M7 review nit #2): a duplicate index leaves another position
    unfilled — the voyage/cohere scatter now returns None on that gap (parity with
    _scatter_vertex_records), never a silent 0.0 for the missing passage. A clean
    all-unique response still scatters correctly (no regression to the live path)."""
    # index 0 twice, index 1 missing (len == n but a gap) → None
    gap = {"data": [{"index": 0, "relevance_score": 0.1},
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 2, "relevance_score": 0.9}]}
    assert rerank._scatter_relevance_scores(gap, 3) is None
    ok = {"results": [{"index": 2, "relevance_score": 0.9},
                      {"index": 0, "relevance_score": 0.1},
                      {"index": 1, "relevance_score": 0.5}]}
    assert rerank._scatter_relevance_scores(ok, 3) == [0.1, 0.5, 0.9]


def test_vllm_reachable_still_probes_localhost(monkeypatch):
    """reachable() for vllm keeps the localhost /v1/models probe."""
    seen = []

    def fake_get(url, timeout=None):
        seen.append((url, timeout))
        return FakeResp(200, {})

    monkeypatch.setattr(rerank.requests, "get", fake_get)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"):
        assert rerank.reachable() is True
    assert seen and seen[0][0] == "http://h:1/v1/models"


def test_service_reachable_backcompat_wrapper(monkeypatch):
    """service_reachable() stays a thin wrapper over the localhost /v1/models
    probe — the fresh-GPU-box detector, independent of the selected provider."""
    seen = []

    def fake_get(url, timeout=None):
        seen.append(url)
        return FakeResp(200, {})

    monkeypatch.setattr(rerank.requests, "get", fake_get)
    with pin_cfg("rerank", provider="voyage", base_url="http://h:2",
                 model="qwen3-reranker-0.6b"):
        assert rerank.service_reachable() is True
    assert seen and seen[0] == "http://h:2/v1/models"


# ── GET /rerank/status ────────────────────────────────────────────────────────

# Pre-M3 (legacy) status keys — every one must survive (additive contract, so
# the wizard's current bind + old frontends keep working).
LEGACY_STATUS_KEYS = {
    "enabled", "provider", "model", "model_id", "base_url", "configured",
    "preflight", "available", "candidate_n", "passage_chars", "models",
    # M13 additive keys for the wizard's reranker block:
    "gpu", "service_reachable",
}
# M3.3 additive keys: hardware tier/ram + per-provider auth & reachability.
# M10.1 additive: model_catalog (per-model selector metadata for the wizard).
STATUS_KEYS = LEGACY_STATUS_KEYS | {
    "tier", "ram_mb", "reachable", "auth_kind", "key_present",
    "preflight_ceiling_ms", "model_catalog",
}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _pin_hardware(monkeypatch, gpu: bool):
    """Deterministic host-hardware probe (the dev box HAS the GPU, CI hasn't).
    Includes the M1 `tier` field so status()'s M3.3 tier passthrough is real."""
    vram = 16380 if gpu else None
    ram = 32768
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": gpu, "gpu_name": "RTX Test" if gpu else None,
        "vram_mb": vram, "ram_mb": ram,
        "source": "nvidia-smi" if gpu else "none",
        "tier": rerank.hardware.derive_tier(gpu, vram, ram),
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


# ── M3.3 status() additive fields ─────────────────────────────────────────────

def test_status_carries_tier_ram_and_key_present(client, monkeypatch):
    """status() exposes hardware tier + ram_mb and per-provider auth
    (auth_kind/key_present) + reachability for a keyed cloud provider."""
    _pin_hardware(monkeypatch, gpu=False)          # MID tier, ram 32768
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(key_env="VOYAGE_API_KEY", ceiling=1200))
    monkeypatch.setenv("VOYAGE_API_KEY", "present")
    monkeypatch.setattr(rerank, "score", lambda q, p: None)  # no cloud network
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"), \
         pin_cfg("retrieval", rerank_enabled=None):
        body = client.get("/rerank/status").json()
    assert body["tier"] == "MID"
    assert body["ram_mb"] == 32768
    assert body["auth_kind"] == "bearer_env"
    assert body["key_present"] is True
    assert body["reachable"] is True               # key present, no network
    assert body["preflight_ceiling_ms"] == 1200.0


def test_status_keeps_all_legacy_keys(client, monkeypatch):
    """Every pre-M3 status key survives (additive contract)."""
    _pin_hardware(monkeypatch, gpu=False)
    with pin_cfg("rerank", provider="null", base_url=None), \
         pin_cfg("retrieval", rerank_enabled=None, rerank_candidate_n=None):
        body = client.get("/rerank/status").json()
    assert LEGACY_STATUS_KEYS <= set(body)


def test_status_never_500s(client, monkeypatch):
    """Malformed numeric config must not 500 status() (ties to M3.1's
    resilience — get_settings AND status's own reads degrade to defaults)."""
    _pin_hardware(monkeypatch, gpu=False)
    monkeypatch.setattr(rerank, "score", lambda q, p: None)  # no vllm network
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="bad", passage_chars="nope"), \
         pin_cfg("retrieval", rerank_enabled="maybe", rerank_candidate_n="lots"):
        r = client.get("/rerank/status")
    assert r.status_code == 200
    body = r.json()
    assert body["candidate_n"] == 40               # malformed → default
    assert body["passage_chars"] == 4096           # malformed → default
    assert body["enabled"] is False                # malformed → default


# ── M4: sidecar > config > default resolution + live keys + is_enabled ────────

def test_get_settings_prefers_sidecar_over_config():
    """A rerank.json selection wins over config.ini [rerank] for provider AND
    model (sidecar > config > default). Config here says vllm/0.6b; the sidecar
    picks a cloud model → get_settings resolves the sidecar's."""
    monkeypatch_entry = _fake_cloud_entry(key_env="VOYAGE_API_KEY")
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(rerank.RERANK_MODELS, "fake-voyage", monkeypatch_entry)
        _write_sidecar({"enabled": True, "provider": "voyage",
                        "model": "fake-voyage"})
        with pin_cfg("rerank", provider="vllm", base_url="http://x:1",
                     model="qwen3-reranker-0.6b"):
            s = rerank.get_settings()
    assert s["provider"] == "voyage"
    assert s["model"] == "fake-voyage"
    assert s["model_id"] == "voyage-rerank"        # resolved off the sidecar pick
    assert s["auth_kind"] == "bearer_env"          # descriptor flows from it too
    assert s["key_env"] == "VOYAGE_API_KEY"


def test_get_settings_falls_back_to_config_when_no_sidecar():
    """No sidecar (empty tmp stores dir) → today's config reads stand."""
    from Orchestrator.embeddings import store as _store
    assert _store.get_rerank_selection() is None   # sanity: nothing on disk
    with pin_cfg("rerank", provider="vllm", base_url="http://x:1",
                 model="qwen3-reranker-4b"):
        s = rerank.get_settings()
    assert s["provider"] == "vllm"
    assert s["model"] == "qwen3-reranker-4b"
    assert s["model_id"] == "Qwen/Qwen3-Reranker-4B"


def test_get_settings_partial_sidecar_backfills_provider_from_config():
    """A sidecar missing `provider` (or with a blank one) does not blank the
    provider — the config value still fills the gap (defensive resolution)."""
    _write_sidecar({"enabled": True, "model": "qwen3-reranker-4b"})
    with pin_cfg("rerank", provider="vllm", base_url="http://x:1",
                 model="qwen3-reranker-0.6b"):
        s = rerank.get_settings()
    assert s["provider"] == "vllm"                  # from config (sidecar silent)
    assert s["model"] == "qwen3-reranker-4b"        # from sidecar


def test_bearer_key_resolved_from_env_fresh(monkeypatch):
    """Keys resolve via os.getenv(key_env) at CALL time, NOT via config.py's
    frozen module constants (bound at import). Set the env AFTER import → the
    key is seen (key_present True) → proves the resolution is live, so a
    wizard-mirrored os.environ write takes effect with no restart."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(key_env="VOYAGE_API_KEY"))
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"):
        assert rerank.get_settings()["key_present"] is False   # not yet set
        monkeypatch.setenv("VOYAGE_API_KEY", "vk-live-after-import")
        assert rerank.get_settings()["key_present"] is True     # seen live


def test_missing_key_configured_but_key_present_false(monkeypatch):
    """A selected cloud provider is `configured` (M2 doesn't gate cloud on a
    key) yet key_present stays False until the env var exists."""
    monkeypatch.setitem(rerank.RERANK_MODELS, "fake-voyage",
                        _fake_cloud_entry(key_env="VOYAGE_API_KEY"))
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pin_cfg("rerank", provider="voyage", model="fake-voyage"):
        s = rerank.get_settings()
        assert rerank._configured(s) is True
        assert s["key_present"] is False


def test_is_enabled_sidecar_then_config():
    """is_enabled(): the sidecar's `enabled` wins when the sidecar exists (even
    over a contradicting config); absent sidecar → [retrieval] rerank_enabled."""
    # sidecar enabled=true beats config false
    _write_sidecar({"enabled": True, "provider": "vllm", "model": "x"})
    with pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.is_enabled() is True
    # sidecar enabled=false beats config true
    _write_sidecar({"enabled": False, "provider": "vllm", "model": "x"})
    with pin_cfg("retrieval", rerank_enabled="true"):
        assert rerank.is_enabled() is False


def test_is_enabled_no_sidecar_reads_config():
    """No sidecar → the resilient [retrieval] rerank_enabled read (default
    False on a fresh box with the option absent)."""
    with pin_cfg("retrieval", rerank_enabled="true"):
        assert rerank.is_enabled() is True
    with pin_cfg("retrieval", rerank_enabled=None):
        assert rerank.is_enabled() is False


def test_is_enabled_malformed_config_defaults_false():
    """A malformed [retrieval] rerank_enabled degrades to False (never raises —
    the _cfg_bool resilient reader), same as status()."""
    with pin_cfg("retrieval", rerank_enabled="maybe"):
        assert rerank.is_enabled() is False


def test_status_enabled_tracks_is_enabled_sidecar_over_config(monkeypatch):
    """M8 live-review fix: status()['enabled'] mirrors the ACTUAL retrieve() gate
    (is_enabled → sidecar > config), so the wizard can't show "disabled" while a
    sidecar selection has rerank ON. A sidecar {enabled:true} that CONTRADICTS
    config rerank_enabled=false → status enabled True; no sidecar + config false →
    False (backward-compatible fallback)."""
    # status() runs preflight() for a configured cloud provider → guard the
    # network hard (the box may hold a real COHERE key; the autouse fixture only
    # stubs .get). No key + no post → preflight None, zero network.
    def _no_net(*a, **k):
        raise ConnectionError("test: network disabled")
    monkeypatch.setattr(rerank.requests, "post", _no_net)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    # sidecar ON, config OFF → status.enabled follows the sidecar (the live gate).
    _write_sidecar({"enabled": True, "provider": "cohere",
                    "model": "cohere-rerank-4"})
    with pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.is_enabled() is True
        assert rerank.status()["enabled"] is True


def test_status_enabled_falls_back_to_config_when_no_sidecar(monkeypatch):
    """Backward-compatible: no sidecar → status()['enabled'] is the [retrieval]
    rerank_enabled config read, exactly as pre-M8 (the empty tmp stores dir makes
    _load_sidecar None; belt-and-suspenders monkeypatch it too)."""
    monkeypatch.setattr(rerank, "_load_sidecar", lambda: None)
    with pin_cfg("rerank", provider="null"), \
         pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.status()["enabled"] is False
    with pin_cfg("rerank", provider="null"), \
         pin_cfg("retrieval", rerank_enabled="true"):
        assert rerank.status()["enabled"] is True


def test_retrieval_gate_uses_is_enabled():
    """M8 wires the retrieve() rerank gate to rerank.is_enabled() (sidecar>config)
    so enabling via the selector sidecar (POST /rerank/select) turns the rerank
    stage on without a config.ini edit. Replaces M4's
    test_retrieval_gate_unchanged_in_m4 — the gate moved from a config-only read
    to is_enabled() in M8."""
    import inspect

    from Orchestrator import retrieval
    src = inspect.getsource(retrieval)
    assert "_rerank.is_enabled()" in src
    # the old config-only enablement gate is gone (is_enabled resolves it now).
    assert 'CFG.getboolean("retrieval", "rerank_enabled"' not in src


# ── M6: LLM-as-reranker (listwise, single completion) ─────────────────────────
# The frontier chat keys we already hold become a cheap, keyless-beyond-existing
# cloud fallback tier: ONE non-streaming completion returns a permutation of the
# candidate indices, mapped to synthetic descending scores. Framed honestly as
# NOT a purpose-trained ranker. Tests MOCK requests.post — no real LLM calls.

_LLM_SLUGS = ["llm-rerank-gemini-flash", "llm-rerank-gpt-mini",
              "llm-rerank-claude-haiku", "llm-rerank-grok"]


def _llm_provider_response(model_slug, text):
    """A provider-appropriately-shaped FakeResp whose extracted completion text
    == `text`, keyed off the model's key_env (Gemini generateContent / Anthropic
    messages / OpenAI+xAI chat-completions)."""
    key_env = rerank.RERANK_MODELS[model_slug]["key_env"]
    if key_env in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        return FakeResp(200, {"candidates": [
            {"content": {"parts": [{"text": text}]}}]})
    if key_env == "ANTHROPIC_API_KEY":
        return FakeResp(200, {"content": [{"type": "text", "text": text}]})
    return FakeResp(200, {"choices": [{"message": {"content": text}}]})


def _llm_prompt_from_body(model_slug, body):
    """Pull the user prompt string out of a captured request body per provider."""
    key_env = rerank.RERANK_MODELS[model_slug]["key_env"]
    if key_env in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        return body["contents"][0]["parts"][0]["text"]
    return body["messages"][0]["content"]


@pytest.mark.parametrize("slug", _LLM_SLUGS)
def test_llm_entries_schema_and_framing(slug):
    """The 4 M6 entries: provider=llm, frontier_key auth, cloud, all tiers,
    honest quality_note, per-provider ceiling/passage_n, NO Qwen prefix."""
    e = rerank.RERANK_MODELS[slug]
    assert e["provider"] == "llm"
    assert e["auth_kind"] == "frontier_key"
    assert e["privacy"] == "cloud"
    assert set(e["tiers"]) == {"LOW", "MID", "HIGH"}
    assert e["key_env"] in {
        "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"}
    assert "not a purpose-trained ranker" in e["quality_note"]
    assert "query_instruction" not in e            # llm gets no Qwen prefix
    assert e["preflight_ceiling_ms"] == 4000
    assert e["preflight_passage_n"] == 1


@pytest.mark.parametrize("slug", _LLM_SLUGS)
def test_score_llm_valid_permutation_maps_to_aligned_scores(monkeypatch, slug):
    """A valid index permutation maps to synthetic descending scores aligned to
    the ORIGINAL passage order. Proven for every frontier provider family (the 4
    request/response shapes)."""
    key_env = rerank.RERANK_MODELS[slug]["key_env"]
    monkeypatch.setenv(key_env, "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(slug, '{"ranking": [2, 0, 1]}'))
    with pin_cfg("rerank", provider="llm", model=slug):
        got = rerank.score("q", ["p0", "p1", "p2"])
    assert got is not None and len(got) == 3
    # order [2,0,1]: rank0->passage2 (1.0), rank1->passage0 (0.5), rank2->passage1 (1/3)
    assert got[2] == 1.0
    assert got[2] > got[0] > got[1]
    assert got == [0.5, 1.0 / 3.0, 1.0]


def test_score_llm_malformed_json_returns_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(
            "llm-rerank-gpt-mini", "not json at all {{"))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


def test_score_llm_missing_or_duplicate_index_returns_none(monkeypatch):
    """[0,0,1] duplicates 0 and misses 2 → not a length-3 permutation → None."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(
            "llm-rerank-gpt-mini", '{"ranking": [0, 0, 1]}'))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


def test_score_llm_out_of_range_index_returns_none(monkeypatch):
    """Index 5 is out of range for 3 passages → None (defensive contract)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(
            "llm-rerank-gpt-mini", '{"ranking": [5, 0, 1]}'))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


def test_score_llm_wrong_length_returns_none(monkeypatch):
    """A permutation shorter than the passage count → None."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(
            "llm-rerank-gpt-mini", '{"ranking": [0, 1]}'))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


def test_score_llm_truncates_passages_to_snippet(monkeypatch):
    """Each passage is truncated to ≤512 chars before prompting (the biggest
    latency/cost lever). A 1000-char passage appears only as its 512-char head."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    def fake_post(url, json=None, timeout=None, **k):
        captured["body"] = json
        return _llm_provider_response("llm-rerank-gpt-mini", '{"ranking": [0]}')

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        rerank.score("q", ["A" * 1000])
    prompt = _llm_prompt_from_body("llm-rerank-gpt-mini", captured["body"])
    assert ("A" * 512) in prompt          # the 512-char head IS present
    assert ("A" * 513) not in prompt       # nothing beyond 512 chars leaked


def test_score_llm_single_completion(monkeypatch):
    """Exactly ONE non-streaming HTTP completion per rerank call."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _llm_provider_response(
            "llm-rerank-gpt-mini", '{"ranking": [1, 0]}')

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        got = rerank.score("q", ["p0", "p1"])
    # order [1,0]: passage1 rank0 (1.0), passage0 rank1 (0.5)
    assert got == [0.5, 1.0]
    assert calls["n"] == 1


def test_score_llm_missing_key_returns_none(monkeypatch):
    """No frontier key in the env → None, with ZERO HTTP calls (never raises)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call the LLM without a key")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_llm_http_error_returns_none(monkeypatch):
    """A non-200 completion → None (never raises)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_llm_transport_exception_returns_none(monkeypatch):
    """A transport blow-up is swallowed → None (dispatcher never-raise + the
    helper's own defense)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_llm_no_query_instruction_for_llm(monkeypatch):
    """llm entries carry NO query_instruction — the Qwen instruct prefix must
    NOT be prepended (it inverts non-Qwen rankers, RERANK_MODELS discipline)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    def fake_post(url, json=None, timeout=None, **k):
        captured["body"] = json
        return _llm_provider_response("llm-rerank-gpt-mini", '{"ranking": [0]}')

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gpt-mini"):
        rerank.score("fix the truncation", ["some passage"])
    prompt = _llm_prompt_from_body("llm-rerank-gpt-mini", captured["body"])
    assert "Instruct: Given a search query" not in prompt
    assert "fix the truncation" in prompt


def test_score_llm_bare_array_permutation_parses(monkeypatch):
    """Defensive parse also accepts a bare JSON array (not just {"ranking": ...})
    — models under response_mime_type=application/json may emit either."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: _llm_provider_response(
            "llm-rerank-gemini-flash", "[1, 0]"))
    with pin_cfg("rerank", provider="llm", model="llm-rerank-gemini-flash"):
        got = rerank.score("q", ["p0", "p1"])
    assert got == [0.5, 1.0]                # order [1,0]


# ── M7: dedicated cloud cross-encoders (Voyage / Cohere, bearer REST) ─────────
# The PRIMARY quality cloud path — purpose-trained rerankers over raw REST (no
# SDK deps). {results:[{index, relevance_score}]} scattered back to passage
# positions on ONE scale; None on missing key / HTTP error / count-or-index
# anomaly. Tests MOCK requests.post — no real Voyage/Cohere calls.

# (slug, provider, key_env, url, model_id, count_key) per dedicated cloud reranker.
_DEDICATED_CLOUD = [
    ("voyage-rerank-2.5", "voyage", "VOYAGE_API_KEY",
     "https://api.voyageai.com/v1/rerank", "rerank-2.5", "top_k"),
    ("cohere-rerank-4", "cohere", "COHERE_API_KEY",
     "https://api.cohere.ai/v2/rerank", "rerank-v4.0-pro", "top_n"),
]


def _cloud_body(provider: str, rows: list) -> dict:
    """The provider's REAL response envelope (both live-verified against the
    APIs): Voyage nests the {index, relevance_score} rows under `data` (inside an
    {"object":"list", ..., "model", "usage"} envelope), Cohere under `results`.
    The `data`-vs-`results` divergence is exactly what a wrong-field parse
    (masked by a mock that used the wrong key) got wrong on the live call."""
    if provider == "voyage":
        return {"object": "list", "data": rows, "model": "rerank-2.5",
                "usage": {"total_tokens": 36}}
    return {"results": rows}


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_entry_schema(slug, provider, key_env, url, model_id,
                                      count_key):
    """The two M7 dedicated entries: bearer_env auth, cloud, all tiers, 1200ms
    ceiling, 1-passage probe, NO Qwen prefix, no local footprint."""
    e = rerank.RERANK_MODELS[slug]
    assert e["provider"] == provider
    assert e["auth_kind"] == "bearer_env"
    assert e["key_env"] == key_env
    assert e["privacy"] == "cloud"
    assert set(e["tiers"]) == {"LOW", "MID", "HIGH"}
    assert e["preflight_ceiling_ms"] == 1200
    assert e["preflight_passage_n"] == 1
    assert e["model_id"] == model_id
    assert "query_instruction" not in e          # cloud gets no Qwen prefix
    assert "vram_gb" not in e and "ram_gb" not in e   # cloud → no local footprint


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_scatters_by_index(monkeypatch, slug, provider, key_env,
                                           url, model_id, count_key):
    """Out-of-order {index, relevance_score} rows scatter back to the ORIGINAL
    passage positions; the request carries the raw query + all-passages count."""
    monkeypatch.setenv(key_env, "k-test")
    captured = {}

    def fake_post(u, headers=None, json=None, timeout=None, **k):
        captured["url"], captured["headers"] = u, headers
        captured["json"], captured["timeout"] = json, timeout
        # provider-REAL envelope (Voyage `data`, Cohere `results`).
        return FakeResp(200, _cloud_body(provider, [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.5},
        ]))

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider=provider, model=slug, timeout_s="9"):
        got = rerank.score("the query", ["pA", "pB", "pC"])
    assert got == [0.1, 0.5, 0.9]                # scattered by index
    assert captured["url"] == url
    assert captured["headers"]["Authorization"] == "Bearer k-test"
    assert captured["json"]["model"] == model_id
    assert captured["json"]["query"] == "the query"
    assert captured["json"]["documents"] == ["pA", "pB", "pC"]
    assert captured["json"][count_key] == 3      # request ALL back (no truncation)
    assert captured["timeout"] == 9.0


def test_score_voyage_parses_real_data_envelope(monkeypatch):
    """Regression (live-API corrected): Voyage's real /v1/rerank response nests
    the rows under `data` (NOT `results`) inside an object/model/usage envelope.
    A parse that reads `results` returns None on every real call — this pins the
    `data` shape so that regression can't return. Verified live: HTTP 200 with
    {"object":"list","data":[{"relevance_score":..,"index":..},...],"usage":..}."""
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    real_envelope = {
        "object": "list",
        "data": [
            {"relevance_score": 0.890625, "index": 3},
            {"relevance_score": 0.886, "index": 1},
            {"relevance_score": 0.5, "index": 0},
            {"relevance_score": 0.4, "index": 2},
        ],
        "model": "rerank-2.5",
        "usage": {"total_tokens": 36},
    }
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(200, real_envelope))
    with pin_cfg("rerank", provider="voyage", model="voyage-rerank-2.5"):
        got = rerank.score("q", ["p0", "p1", "p2", "p3"])
    # scattered back by index → passage3 highest, passage2 lowest
    assert got == [0.5, 0.886, 0.4, 0.890625]
    assert got is not None and len(got) == 4


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_missing_key_returns_none(monkeypatch, slug, provider,
                                                  key_env, url, model_id,
                                                  count_key):
    """No key in the env → None with ZERO HTTP calls (never raises)."""
    monkeypatch.delenv(key_env, raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call the cloud reranker without a key")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider=provider, model=slug):
        assert rerank.score("q", ["p0", "p1"]) is None


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_http_error_returns_none(monkeypatch, slug, provider,
                                                 key_env, url, model_id,
                                                 count_key):
    """A non-200 response → None (never raises)."""
    monkeypatch.setenv(key_env, "k-test")
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    with pin_cfg("rerank", provider=provider, model=slug):
        assert rerank.score("q", ["p0", "p1"]) is None


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_row_count_mismatch_returns_none(monkeypatch, slug,
                                                         provider, key_env, url,
                                                         model_id, count_key):
    """Fewer results than passages → None (a partial result can't rank on one
    scale)."""
    monkeypatch.setenv(key_env, "k-test")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: FakeResp(200, _cloud_body(
            provider, [{"index": 0, "relevance_score": 1.0}])))
    with pin_cfg("rerank", provider=provider, model=slug):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_dedicated_cloud_out_of_range_index_returns_none(monkeypatch, slug,
                                                        provider, key_env, url,
                                                        model_id, count_key):
    """An out-of-range index (5 for 2 passages) → None, never an IndexError."""
    monkeypatch.setenv(key_env, "k-test")
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: FakeResp(200, _cloud_body(provider, [
            {"index": 5, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1}])))
    with pin_cfg("rerank", provider=provider, model=slug):
        assert rerank.score("q", ["p0", "p1"]) is None


@pytest.mark.parametrize("slug,provider,key_env,url,model_id,count_key",
                         _DEDICATED_CLOUD)
def test_no_query_instruction_for_cloud_rerankers(monkeypatch, slug, provider,
                                                  key_env, url, model_id,
                                                  count_key):
    """Voyage + Cohere carry NO query_instruction and send the RAW query — the
    Qwen instruct prefix must never reach a non-Qwen ranker (it inverts them)."""
    assert "query_instruction" not in rerank.RERANK_MODELS[slug]
    monkeypatch.setenv(key_env, "k-test")
    captured = {}

    def fake_post(u, headers=None, json=None, timeout=None, **k):
        captured["json"] = json
        return FakeResp(200, _cloud_body(
            provider, [{"index": 0, "relevance_score": 0.5}]))

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider=provider, model=slug):
        rerank.score("fix the truncation", ["some passage"])
    assert captured["json"]["query"] == "fix the truncation"
    assert "Instruct: Given a search query" not in captured["json"]["query"]


# ── M7: Vertex semantic-ranker (GCP service-account OAuth) ────────────────────
# Dedicated cross-encoder via the Discovery Engine Ranking API. Creds come from
# the ambient GCP service account (GOOGLE_APPLICATION_CREDENTIALS, set +
# live-mirrored by the credentials upload route). Tests MOCK google.auth.default
# + credentials.refresh + requests.post — NO real google.auth or network.

_VERTEX_SLUG = "vertex-semantic-ranker"
_VERTEX_URL_TMPL = ("https://discoveryengine.googleapis.com/v1/projects/{proj}"
                    "/locations/global/rankingConfigs/"
                    "default_ranking_config:rank")


class _FakeCreds:
    """Stand-in for google.auth Credentials: refresh() sets .token (or raises)."""

    def __init__(self, token="vertex-token", refresh_raises=False):
        self._token, self._raise = token, refresh_raises
        self.token = None

    def refresh(self, request):
        if self._raise:
            raise RuntimeError("refresh failed")
        self.token = self._token


def _patch_vertex_auth(monkeypatch, creds, project):
    """Patch google.auth.default → (creds, project) — the real
    _vertex_token_and_project then runs (refresh + project resolution)."""
    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (creds, project))


def test_vertex_entry_schema():
    """The M7.2 entry: gcp_service_account auth, no key_env, cloud, all tiers,
    1500ms ceiling, honest 'Advanced' note, NO Qwen prefix, no local footprint."""
    e = rerank.RERANK_MODELS[_VERTEX_SLUG]
    assert e["provider"] == "vertex"
    assert e["auth_kind"] == "gcp_service_account"
    assert e["key_env"] is None
    assert e["privacy"] == "cloud"
    assert set(e["tiers"]) == {"LOW", "MID", "HIGH"}
    assert e["preflight_ceiling_ms"] == 1500
    assert e["preflight_passage_n"] == 1
    assert e["model_id"] == "semantic-ranker-default-004"
    assert "query_instruction" not in e
    assert "vram_gb" not in e and "ram_gb" not in e
    assert "Advanced" in e["quality_note"]


def test_score_vertex_maps_by_record_id(monkeypatch):
    """Out-of-order {id, score} records map back onto passage positions by the
    str(original_index) id we sent; the request carries the raw query + all
    records (no topN truncation)."""
    monkeypatch.setattr(rerank, "_vertex_token_and_project",
                        lambda s: ("tok", "my-proj"))
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **k):
        captured["url"], captured["headers"] = url, headers
        captured["json"], captured["timeout"] = json, timeout
        return FakeResp(200, {"records": [
            {"id": "2", "score": 0.9},
            {"id": "0", "score": 0.1},
            {"id": "1", "score": 0.5},
        ]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG, timeout_s="9"):
        got = rerank.score("the query", ["pA", "pB", "pC"])
    assert got == [0.1, 0.5, 0.9]                # scattered by record id
    assert captured["url"] == _VERTEX_URL_TMPL.format(proj="my-proj")
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["headers"]["X-Goog-User-Project"] == "my-proj"
    assert captured["json"]["model"] == "semantic-ranker-default-004"
    assert captured["json"]["query"] == "the query"
    assert [r["id"] for r in captured["json"]["records"]] == ["0", "1", "2"]
    assert [r["content"] for r in captured["json"]["records"]] == \
        ["pA", "pB", "pC"]
    assert "topN" not in captured["json"]        # request ALL back
    assert captured["timeout"] == 9.0


def test_score_vertex_project_id_env_override(monkeypatch):
    """The real _vertex_token_and_project mints a token (google.auth.default +
    refresh) and prefers VERTEX_PROJECT_ID over the SA's default project."""
    _patch_vertex_auth(monkeypatch, _FakeCreds(token="tok-live"),
                       "sa-default-proj")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "override-proj")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **k):
        captured["url"], captured["headers"] = url, headers
        return FakeResp(200, {"records": [{"id": "0", "score": 0.7}]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        got = rerank.score("q", ["only passage"])
    assert got == [0.7]
    assert "projects/override-proj/" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer tok-live"
    assert captured["headers"]["X-Goog-User-Project"] == "override-proj"


def test_score_vertex_no_creds_returns_none(monkeypatch):
    """google.auth.default raising (no ambient creds) → None, ZERO HTTP calls,
    never raises."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def raise_no_creds(*a, **k):
        raise DefaultCredentialsError("no creds")

    monkeypatch.setattr(google.auth, "default", raise_no_creds)

    def boom(*a, **k):
        raise AssertionError("must not POST without creds")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_vertex_refresh_failure_returns_none(monkeypatch):
    """A credentials.refresh() blow-up → None, ZERO HTTP calls (never raises)."""
    _patch_vertex_auth(monkeypatch, _FakeCreds(refresh_raises=True), "my-proj")

    def boom(*a, **k):
        raise AssertionError("must not POST when refresh fails")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_vertex_no_project_returns_none(monkeypatch):
    """Creds refresh OK but neither a default project nor VERTEX_PROJECT_ID →
    None (no project = nowhere to send the request)."""
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    _patch_vertex_auth(monkeypatch, _FakeCreds(), None)

    def boom(*a, **k):
        raise AssertionError("must not POST without a project")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_vertex_http_error_returns_none(monkeypatch):
    """A non-200 ranking response → None (never raises)."""
    monkeypatch.setattr(rerank, "_vertex_token_and_project",
                        lambda s: ("tok", "proj"))
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_score_vertex_count_mismatch_returns_none(monkeypatch):
    """Fewer records than passages → None (a partial result can't rank on one
    scale)."""
    monkeypatch.setattr(rerank, "_vertex_token_and_project",
                        lambda s: ("tok", "proj"))
    monkeypatch.setattr(
        rerank.requests, "post",
        lambda *a, **k: FakeResp(200, {"records": [{"id": "0", "score": 1.0}]}))
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        assert rerank.score("q", ["p0", "p1", "p2"]) is None


def test_score_vertex_truncates_record_content(monkeypatch):
    """Each record's content is truncated to the ~1024-token safe char budget
    before sending (Vertex's per-record limit)."""
    monkeypatch.setattr(rerank, "_vertex_token_and_project",
                        lambda s: ("tok", "proj"))
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **k):
        captured["json"] = json
        return FakeResp(200, {"records": [{"id": "0", "score": 0.5}]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    with pin_cfg("rerank", provider="vertex", model=_VERTEX_SLUG):
        rerank.score("q", ["Z" * 5000])
    content = captured["json"]["records"][0]["content"]
    assert len(content) == rerank._VERTEX_CONTENT_CHARS
    assert len(content) < 5000


# ── M10.1: model_catalog() — per-model selector metadata for the wizard/Portal ──
# The live /rerank/status `models` is a flat slug list with no per-model
# provider/tiers/key_present, so the tier-driven selector the plan describes
# cannot be built from it. model_catalog() exposes that metadata additively;
# key_present is resolved FRESH per model so a just-pasted+mirrored cloud key
# gates selectability with no restart.

def test_model_catalog_has_one_entry_per_model_with_required_fields():
    cat = rerank.model_catalog()
    assert isinstance(cat, list)
    assert {c["slug"] for c in cat} == set(rerank.RERANK_MODELS)
    required = {"slug", "provider", "label", "tiers", "privacy",
                "key_env", "key_present", "cost_note", "quality_note"}
    for c in cat:
        assert required <= set(c), f"{c['slug']} missing {required - set(c)}"
        assert isinstance(c["tiers"], list)


def test_model_catalog_key_present_reads_env_fresh(monkeypatch):
    """A cloud model's key_present tracks os.getenv(key_env) at call time —
    a live-mirrored paste gates selectability without a restart."""
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-live")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    cat = {c["slug"]: c for c in rerank.model_catalog()}
    assert cat["voyage-rerank-2.5"]["key_present"] is True
    assert cat["cohere-rerank-4"]["key_present"] is False


def test_model_catalog_local_models_need_no_key(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    cat = {c["slug"]: c for c in rerank.model_catalog()}
    local = cat["qwen3-reranker-0.6b"]
    assert local["privacy"] == "local"
    assert local["key_env"] is None
    assert local["key_present"] is False


def test_status_exposes_model_catalog_additively():
    """status() carries the catalog AND keeps the flat `models` slug list
    (M11/M12/M13 + old frontends bind to `models` — must not change shape)."""
    st = rerank.status()
    assert st["models"] == sorted(rerank.RERANK_MODELS.keys())  # unchanged
    assert {c["slug"] for c in st["model_catalog"]} == set(rerank.RERANK_MODELS)


def test_model_catalog_tiers_match_registry():
    """Tier gating in the UI relies on tiers being the registry's tiers verbatim."""
    cat = {c["slug"]: c for c in rerank.model_catalog()}
    assert cat["cohere-rerank-4"]["tiers"] == ["LOW", "MID", "HIGH"]
    assert cat["qwen3-reranker-0.6b"]["tiers"] == ["HIGH"]       # GPU only
    assert cat["qwen3-reranker-0.6b-cpu"]["tiers"] == ["MID"]    # CPU opt-in


# ── M13: fresh LOW-box portability + rollback matrix (audit A13) ──────────────
# The consolidation regression: this box IS the fresh LOW box (no GPU, no
# reranker deps, no [rerank] config, no rerank.json — the autouse
# _isolate_rerank_sidecar points every test at an EMPTY tmp stores dir). These
# pin that the reranker ships INERT and import-clean out of the box, and that
# every rollback lever independently forces it off.

def test_requirements_has_no_torch_or_cloud_reranker_sdk():
    """A fresh box installs from requirements.txt — it must NOT pull torch (the
    reranker is CPU/GPU-OPTIONAL, never a base dep; the CPU CrossEncoder path
    lazy-imports sentence-transformers) nor any cloud reranker SDK (Voyage /
    Cohere / Vertex are reached over RAW REST). Pin so dependency creep can't
    make the base install heavy or GPU-coupled (audit A13 fresh-box rule)."""
    from pathlib import Path
    req = Path(__file__).resolve().parents[2] / "requirements.txt"
    assert req.exists(), f"requirements.txt not found at {req}"
    pkgs = []
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if line:
            # normalize the leading distribution name (strip any version spec)
            import re as _re
            pkgs.append(_re.split(r"[<>=~!\[ ]", line, 1)[0])
    for banned in ("torch", "sentence-transformers", "sentence_transformers",
                   "cohere", "voyageai", "google-cloud-discoveryengine"):
        assert banned not in pkgs, (
            f"{banned} must NOT be a base requirement (fresh-box portability)")


def test_fresh_low_box_heavy_deps_absent_but_modules_import_clean():
    """rerank.py + retrieval.py imported successfully at collection WITHOUT
    torch / sentence-transformers / a cloud reranker SDK present — proving they
    do not import any heavy dep at module top (the CPU path lazy-imports; cloud
    paths use raw REST). Pin the absence AS a regression so a stray top-level
    `import torch` (which would crash import on a fresh box) can't slip in."""
    import importlib
    import importlib.util
    import sys

    # This IS a fresh LOW box: the heavy stack is genuinely not installed.
    for dep in ("torch", "sentence_transformers", "cohere", "voyageai"):
        assert importlib.util.find_spec(dep) is None, (
            f"{dep} unexpectedly installed — the fresh-box import test is moot")

    # The modules under test imported anyway (they are in sys.modules) and a
    # fresh import raises NO ImportError with the heavy deps absent.
    assert "Orchestrator.rerank" in sys.modules
    importlib.import_module("Orchestrator.retrieval")   # no ImportError
    importlib.import_module("Orchestrator.rerank")      # idempotent, clean


def test_fresh_low_box_reranker_is_inert():
    """No [rerank] section + no rerank.json (empty tmp stores) + no deps →
    get_settings resolves the null default, is_enabled False, score() inert
    (None), available() False WITHOUT probing anything. The whole module is a
    no-op — the fresh-box default costs one config read and touches no network."""
    from Orchestrator.embeddings import store as _store
    assert _store.get_rerank_selection() is None        # no sidecar on disk
    with pin_cfg("rerank", provider=None, base_url=None, model=None), \
         pin_cfg("retrieval", rerank_enabled=None):
        s = rerank.get_settings()
        assert s["provider"] == "null"
        assert rerank.is_enabled() is False
        assert rerank.score("q", ["p0", "p1"]) is None
        assert rerank.available() is False              # null → no preflight probe


def test_fresh_low_box_cpu_path_inert_no_import_error(monkeypatch):
    """The CPU provider selected on a box with NO sentence-transformers/torch:
    _load_cpu_model lazy-imports, catches the absent dep, and returns None →
    score() returns None (inert), never an ImportError. Belt-and-braces evicts
    any cached module so the lazy import genuinely resolves against absent deps."""
    import sys
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        assert rerank._load_cpu_model("Qwen/Qwen3-Reranker-0.6B") is None
        assert rerank.score("q", ["p0", "p1"]) is None   # inert, no raise


# ── rollback matrix: every lever independently forces the reranker OFF ────────

def test_rollback_delete_sidecar_falls_back_to_config():
    """Delete rerank.json (the wizard/Portal selection) → resolution falls back
    to config.ini [rerank] / the null default. A sidecar that enabled a cloud
    provider vanishes → get_settings + is_enabled resolve config-only again."""
    _write_sidecar({"enabled": True, "provider": "cohere",
                    "model": "cohere-rerank-4"})
    with pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.is_enabled() is True              # sidecar governs
    # Roll back: remove the sidecar file entirely.
    from pathlib import Path

    from Orchestrator import config as _config
    from Orchestrator.embeddings import store as _store
    Path(_config.EMBEDDINGS_STORES_DIR, "rerank.json").unlink(missing_ok=True)
    assert _store.get_rerank_selection() is None
    # Now config alone governs: null provider + rerank_enabled false → off.
    with pin_cfg("rerank", provider=None), \
         pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.is_enabled() is False
        assert rerank.get_settings()["provider"] == "null"


def test_rollback_config_flag_false_forces_off_without_sidecar():
    """The [retrieval] rerank_enabled=false lever: with NO sidecar it force-offs
    the reranker regardless of a configured provider (the config rollback path
    an operator uses when there is no wizard selection to clear)."""
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1"), \
         pin_cfg("retrieval", rerank_enabled="false"):
        assert rerank.is_enabled() is False


def test_rollback_sidecar_enabled_false_forces_off_over_config_true():
    """The sidecar rollback lever: a rerank.json with enabled=false wins over a
    config rerank_enabled=true (the wizard 'turn it off' toggle disables even a
    config that says on)."""
    _write_sidecar({"enabled": False, "provider": "vllm", "model": "x"})
    with pin_cfg("retrieval", rerank_enabled="true"):
        assert rerank.is_enabled() is False


@pytest.mark.parametrize("provider,slug,key_env", [
    ("voyage", "voyage-rerank-2.5", "VOYAGE_API_KEY"),
    ("cohere", "cohere-rerank-4", "COHERE_API_KEY"),
    ("llm", "llm-rerank-gpt-mini", "OPENAI_API_KEY"),
])
def test_rollback_cloud_provider_inert_without_its_key(monkeypatch, provider,
                                                       slug, key_env):
    """Each keyed cloud provider is independently INERT when its key_env is
    unset — score() returns None with ZERO HTTP calls (rolling back a key
    disables just that provider, never crashes, never leaks a call)."""
    monkeypatch.delenv(key_env, raising=False)

    def boom(*a, **k):
        raise AssertionError(f"{provider} must not call out without its key")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider=provider, model=slug):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_rollback_vertex_inert_without_sa_creds(monkeypatch):
    """Vertex (gcp_service_account, no key_env) is inert when the ambient SA
    creds are absent: _vertex_token_and_project returns (None, None) →
    score() None, ZERO HTTP calls."""
    monkeypatch.setattr(rerank, "_vertex_token_and_project",
                        lambda s: (None, None))

    def boom(*a, **k):
        raise AssertionError("vertex must not POST without creds")

    monkeypatch.setattr(rerank.requests, "post", boom)
    with pin_cfg("rerank", provider="vertex", model="vertex-semantic-ranker"):
        assert rerank.score("q", ["p0", "p1"]) is None


def test_tier_is_advisory_only_never_gates_retrieval():
    """The hardware `tier` (LOW/MID/HIGH) is ADVISORY: it steers wizard copy but
    NEVER blocks retrieval. Pin (1) retrieval.py holds no tier/hardware coupling
    at all (retrieve() never reads it), and (2) a LOW-tier box still exposes
    tier as an informational status field (not a gate)."""
    import inspect

    from Orchestrator import retrieval as _retrieval
    src = inspect.getsource(_retrieval)
    assert "tier" not in src, "retrieve() must not consult the hardware tier"
    assert "derive_tier" not in src
    assert "hardware" not in src
    # tier surfaces in status() as advisory metadata only (a plain string).
    tier = rerank.hardware.probe().get("tier")
    assert tier in {"LOW", "MID", "HIGH"}
