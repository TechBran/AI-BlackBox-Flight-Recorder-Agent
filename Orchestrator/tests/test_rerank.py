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
    assert set(rerank.RERANK_MODELS) == {"qwen3-reranker-0.6b", "qwen3-reranker-4b"}
    for slug, entry in rerank.RERANK_MODELS.items():
        assert slug == slug.lower() and "_" not in slug, "slugs are kebab-case"
        for field in ("provider", "model_id", "label", "vram_gb",
                      "max_input_tokens", "quality_note"):
            assert field in entry, f"{slug} missing {field}"
        assert entry["provider"] == "vllm"
        assert isinstance(entry["vram_gb"], float)


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
STATUS_KEYS = LEGACY_STATUS_KEYS | {
    "tier", "ram_mb", "reachable", "auth_kind", "key_present",
    "preflight_ceiling_ms",
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
