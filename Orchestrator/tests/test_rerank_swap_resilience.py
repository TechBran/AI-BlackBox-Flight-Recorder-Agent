"""On-box reranker vs llama-swap: timing, honest capability, and honest status.

Ground truth these tests encode (measured on MS02, 2026-08-11):

  * llama-swap HOLDS a /v1/rerank request while it evicts a group and cold-loads
    the reranker member. A slow READ is the stack working, exactly as
    Orchestrator/embeddings/providers.py documents for LOCALSTACK_READ_TIMEOUT_S.
    The old flat 15s [rerank] timeout_s aborted work llama-swap was still doing
    (a real 40x4096-char rerank extrapolates to ~16s warm, ~19s across a swap).
  * The 1-short-passage preflight was STRUCTURALLY BLIND: that passage stayed
    under the llama.cpp batch size, so /rerank/status reported available:true /
    preflight ok @76.6ms while EVERY production-sized rerank returned HTTP 500.
  * A production-SHAPED synthetic probe does see that failure — by buying it
    with a real ~16s rerank (21.3s cold) on the first search after every
    restart, under _preflight_lock, with concurrent searches queued behind it.
    So capability is now EVIDENCE FROM REAL TRAFFIC: score() records every real
    outcome and the state is ok / failed / unknown, where `unknown` (cold start)
    is truthful, keeps the gate OPEN, and is never reported as ok.
  * The un-reranked fallthrough was logged once per process, so in the
    long-lived service the degradation was invisible after the first search.
  * /rerank/status reported the legacy vLLM :8091 seam as `base_url` with
    service_reachable:false while scoring actually resolved through
    local_stack.base_url() to :9098 — both indicators inverted from reality.
"""
import contextlib

import pytest

from Orchestrator import rerank
from Orchestrator.config import CFG


# ── harness ───────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def pin_cfg(section: str, **keys):
    """Pin options on the in-process CFG (value None = ensure ABSENT);
    restore exactly afterwards. Mirrors test_rerank.py's helper."""
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


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class Clock:
    """Deterministic monotonic() stand-in; advance by mutating .t."""
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _clean_module_state():
    rerank.reset_preflight()
    yield
    rerank.reset_preflight()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test here may touch the real network. GET is the :8091 legacy probe
    (service_reachable + the §5.2 co-run refusal) — refused by default."""
    def refuse(*a, **k):
        raise ConnectionError("test: network disabled")
    monkeypatch.setattr(rerank.requests, "get", refuse)


@pytest.fixture(autouse=True)
def _isolate_sidecar(tmp_path, monkeypatch):
    from Orchestrator import config as _config
    monkeypatch.setattr(_config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "stores"))


def _pin_localstack(monkeypatch, base="http://127.0.0.1:9098/v1", healthy=True):
    monkeypatch.setattr(rerank, "_localstack_base_url", lambda: base)
    monkeypatch.setattr(rerank, "_localstack_healthy", lambda: healthy)


def _read_deadline(timeout):
    """The READ leg of whatever requests was handed (tuple or scalar)."""
    if isinstance(timeout, (tuple, list)):
        return float(timeout[1])
    return float(timeout)


def _connect_deadline(timeout):
    if isinstance(timeout, (tuple, list)):
        return float(timeout[0])
    return float(timeout)


# ── 1. TIMING: the llama-swap hold must not be aborted ───────────────────────

def test_localstack_read_timeout_covers_a_swap_but_is_bounded():
    """The read cap must outlast the slowest MEASURED legitimate rerank (21.3s
    cold, ~19s across an intra-group swap, MS02 2026-08-11) with real headroom,
    yet stay BOUNDED: score() runs inline on the caller's thread (on /chat/stream
    that is the event loop), so a member that accepts the connection and never
    answers holds every caller for the whole cap. A skipped rerank costs ranking
    quality, never results (score() falls through), so 600s was the wrong trade.
    The connect cap stays short so a dead :9098 front door still fails fast."""
    measured_cold_worst_s = 21.3
    assert rerank.LOCALSTACK_READ_TIMEOUT_S >= 4 * measured_cold_worst_s
    assert rerank.LOCALSTACK_READ_TIMEOUT_S <= 120.0
    assert rerank.LOCALSTACK_CONNECT_TIMEOUT_S <= 5.0
    assert rerank.get_settings()["localstack_read_timeout_s"] == \
        rerank.LOCALSTACK_READ_TIMEOUT_S


def test_localstack_read_timeout_has_its_own_knob():
    """[rerank] localstack_timeout_s tunes the on-box read leg; [rerank] timeout_s
    is the CLOUD deadline and must no longer silently change the on-box one
    (setting it to 15 used to drop the on-box read back to the exact 15s abort
    this file exists to prevent)."""
    with pin_cfg("rerank", localstack_timeout_s="42"):
        assert rerank.get_settings()["localstack_read_timeout_s"] == 42.0
    with pin_cfg("rerank", timeout_s="15"):
        s = rerank.get_settings()
        assert s["timeout_s"] == 15.0
        assert s["localstack_read_timeout_s"] == rerank.LOCALSTACK_READ_TIMEOUT_S


def test_slow_swap_but_successful_upstream_scores_instead_of_timing_out(
        monkeypatch):
    """MEASURED: a production-sized rerank across an intra-group swap takes
    ~19s. The upstream is SLOW but WORKING — the client must wait for it, not
    abort at 15s and hand the retriever an un-reranked ranking."""
    swap_seconds = 19.0
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["timeout"] = timeout
        if _read_deadline(timeout) < swap_seconds:
            raise rerank.requests.exceptions.ReadTimeout(
                "llama-swap still loading the reranker member")
        return FakeResp(200, {"results": [{"index": 0, "relevance_score": 0.42}]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    _pin_localstack(monkeypatch)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local", timeout_s=None):
        assert rerank.score("q", ["p"]) == [0.42]
    # connect stays short — a dead front door still fails fast
    assert _connect_deadline(seen["timeout"]) <= 5.0


def test_dead_front_door_still_fails_fast_on_connect(monkeypatch):
    """The long READ cap must not become a long CONNECT cap: a refused :9098
    is a dead stack, not a swap."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["timeout"] = timeout
        raise rerank.requests.exceptions.ConnectTimeout("refused")

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    _pin_localstack(monkeypatch)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local", timeout_s=None):
        assert rerank.score("q", ["p"]) is None
    assert _connect_deadline(seen["timeout"]) <= 5.0
    # The read leg is the bounded swap-tolerant cap, still well past a swap.
    assert _read_deadline(seen["timeout"]) == rerank.LOCALSTACK_READ_TIMEOUT_S
    assert _read_deadline(seen["timeout"]) >= 4 * 21.3


def test_config_timeout_override_still_tunes_the_read_deadline(monkeypatch):
    """An operator can still tune it: [rerank] localstack_timeout_s sets the READ
    leg ([rerank] timeout_s is the cloud deadline and no longer leaks in)."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["timeout"] = timeout
        return FakeResp(200, {"results": [{"index": 0, "relevance_score": 1.0}]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    _pin_localstack(monkeypatch)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local", localstack_timeout_s="45"):
        assert rerank.score("q", ["p"]) == [1.0]
    assert _read_deadline(seen["timeout"]) == 45.0
    assert _connect_deadline(seen["timeout"]) <= 5.0


# ── 2. CAPABILITY IS EVIDENCE FROM REAL TRAFFIC ──────────────────────────────
# A synthetic production-shaped probe answers "does reranking work" correctly
# and charges a real ~16s rerank for the answer, every restart, on the hot path.
# The next real search answers it for free. So: score() records the outcome,
# and the state is one of three honest values — ok (a real rerank succeeded
# inside the TTL), failed (a real rerank failed; reason + batch shape carried),
# unknown (no recent evidence). unknown is NOT ok: the gate stays open and the
# first real search IS the probe.

def _localstack_cfg(**extra):
    return pin_cfg("rerank", provider="localstack",
                   model="qwen3-reranker-8b-local", **extra)


def _record_posts(monkeypatch, status=200):
    """Record every /v1/rerank POST's batch size; 200 scores every document."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        docs = json["documents"]
        calls.append(len(docs))
        if status != 200:
            return FakeResp(status, {"error": "input is too large to process"})
        return FakeResp(200, {"results": [{"index": i, "relevance_score": 1.0}
                                          for i in range(len(docs))]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    return calls


def test_cold_start_is_unknown_and_still_attempts_a_rerank(monkeypatch):
    """No evidence yet is neither ok nor failed — it is `unknown`, and the gate
    stays OPEN so the first real search is the probe. Deciding that must not
    score anything."""
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        pf = rerank.preflight()
        assert pf["state"] == "unknown"
        assert rerank.available() is True
        assert calls == []                   # no synthetic batch was scored
        assert rerank.score("q", ["p"]) == [1.0]
    assert calls == [1]                      # only the real search ran


def test_a_successful_real_score_records_ok_with_no_probe(monkeypatch):
    """A real rerank that worked IS the evidence — recorded with a timestamp,
    bought with zero extra work."""
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) == [1.0]
        ev = rerank.evidence_state()
        assert ev["state"] == "ok"
        assert ev["at"] is not None          # when the evidence was earned
        assert ev["provider"] == "localstack"
        assert rerank.preflight()["state"] == "ok"
        assert rerank.available() is True
    assert calls == [1]                      # the real search, nothing else


def test_a_failed_real_score_records_failed_with_the_reason(monkeypatch):
    """A real failure carries the reason AND the batch shape _note_fallthrough
    already records, and closes the gate."""
    calls = _record_posts(monkeypatch, status=500)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"] * 40) is None
        pf = rerank.preflight()
        assert pf["state"] == "failed"
        assert "500" in pf["reason"] and "40 passages" in pf["reason"]
        assert rerank.evidence_state()["passage_n"] == 40
        assert rerank.available() is False
    assert calls == [40]                     # the failure WAS the real search


def test_repeat_searches_inside_the_ttl_do_no_extra_work(monkeypatch):
    """Evidence inside its TTL is read from memory: no probe, and no per-search
    network poking (the §5.2 co-run check is TTL-cached, not per-search)."""
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    calls = _record_posts(monkeypatch)
    gets = []

    def counting_get(*a, **k):
        gets.append(1)
        raise ConnectionError("nothing on :8091")

    monkeypatch.setattr(rerank.requests, "get", counting_get)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) == [1.0]
        for _ in range(25):
            assert rerank.available() is True
    assert calls == [1]
    assert len(gets) <= 1


def test_evidence_ages_out_to_unknown_instead_of_claiming_ok(monkeypatch):
    """An hours-old success is not a health report. Past the TTL the state is
    `unknown` again — still attempted, just no longer CLAIMED as ok."""
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) == [1.0]
        assert rerank.evidence_state()["state"] == "ok"
        clock.t += rerank._EVIDENCE_OK_TTL_S + 1
        ev = rerank.evidence_state()
        assert ev["state"] == "unknown"
        assert ev["stale"] is True           # a record exists, it just aged out
        assert ev["at"] is not None
        assert rerank.preflight()["state"] == "unknown"
        assert rerank.available() is True    # the next search re-proves it


def test_failed_evidence_recovers_after_its_ttl_without_a_restart(monkeypatch):
    """A wedged reranker costs un-reranked results once per TTL, never a
    restart: past the failure TTL the state decays to unknown and the next real
    search retries."""
    clock = Clock(1000.0)
    monkeypatch.setattr(rerank.time, "monotonic", clock)
    _record_posts(monkeypatch, status=500)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) is None
        assert rerank.available() is False
        clock.t += rerank._PREFLIGHT_FAIL_TTL_S + 1
        assert rerank.preflight()["state"] == "unknown"
        assert rerank.available() is True


def test_the_oversized_batch_failure_is_caught_by_the_first_real_search(
        monkeypatch):
    """THE MS02 BUG, without the synthetic tax. llama.cpp 500s a batch larger
    than its -b/-ub while happily scoring one short passage — so a small probe
    can never see it. The first production-sized REAL search does: the state
    flips to failed and the reason names the batch that broke it."""
    def fake_post(url, json=None, timeout=None):
        docs = json["documents"]
        if sum(len(d) for d in docs) > 20_000:   # over the served batch size
            return FakeResp(500, {"error": "input is too large to process"})
        return FakeResp(200, {"results": [{"index": i, "relevance_score": 1.0}
                                          for i in range(len(docs))]})

    monkeypatch.setattr(rerank.requests, "post", fake_post)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["short"]) == [1.0]     # the blind-probe shape
        assert rerank.preflight()["state"] == "ok"
        assert rerank.score("q", ["x" * 4096] * 40) is None   # production shape
        pf = rerank.preflight()
        assert pf["state"] == "failed"
        assert "40 passages" in pf["reason"] and "163840 chars" in pf["reason"]
        assert rerank.available() is False


def test_the_on_box_batch_providers_never_pay_a_synthetic_probe(monkeypatch):
    """Both on-box front doors are evidence-driven — no restart tax on either."""
    def boom(query, passages):
        raise AssertionError("a synthetic probe was scored")

    monkeypatch.setattr(rerank, "score", boom)
    _pin_localstack(monkeypatch)
    assert rerank._EVIDENCE_PROVIDERS == {"localstack", "vllm"}
    with _localstack_cfg():
        assert rerank.preflight()["state"] == "unknown"
        assert rerank.available() is True
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 model="qwen3-reranker-0.6b"):
        assert rerank.preflight()["state"] == "unknown"
        assert rerank.available() is True


def test_evidence_from_another_selection_does_not_count(monkeypatch):
    """A hand-edited config.ini switches provider/model without going through
    the selector (which resets state). The previous selection's success says
    nothing about the new one — it must read unknown, never ok."""
    _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) == [1.0]
        assert rerank.preflight()["state"] == "ok"
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 model="qwen3-reranker-0.6b"):
        pf = rerank.preflight()
        assert pf["state"] == "unknown"
        assert pf["at"] is None
        assert "different reranker selection" in pf["reason"]


def test_liveness_stays_a_separate_concern_from_capability(monkeypatch):
    """"Can I reach the front door" is NOT "does reranking work": a live stack
    with no scoring evidence is reachable AND unknown, and the liveness read
    never scores anything."""
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": True, "gpu_name": "RTX", "vram_mb": 16380, "ram_mb": 32768,
        "source": "nvidia-smi", "tier": "HIGH"})
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch, healthy=True)
    with _localstack_cfg():
        body = rerank.status()
    assert body["scoring_reachable"] is True      # liveness: front door answers
    assert body["preflight"]["state"] == "unknown"  # capability: unproven
    assert calls == []


def test_status_never_reports_ok_without_real_evidence(monkeypatch):
    """Polling /rerank/status may not cost a 16s rerank, and it may not claim a
    healthy reranker it has no evidence for. `available` stays the GATE (will we
    attempt), the state stays the TRUTH."""
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": True, "gpu_name": "RTX", "vram_mb": 16380, "ram_mb": 32768,
        "source": "nvidia-smi", "tier": "HIGH"})
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        body = rerank.status()
        assert body["preflight"]["state"] == "unknown"
        assert body["evidence"]["state"] == "unknown"
        assert body["evidence"]["at"] is None
        assert body["available"] is True          # optimistic gate, honest state
        assert calls == []                        # status scored NOTHING
        # ... and once a real search proves it, status says ok with a timestamp
        assert rerank.score("q", ["p"]) == [1.0]
        body = rerank.status()
    assert body["preflight"]["state"] == "ok"
    assert body["evidence"]["state"] == "ok"
    assert body["evidence"]["at"] is not None
    assert calls == [1]


def test_cloud_preflight_probe_stays_cheap(monkeypatch):
    """Metered providers keep the cheap registry probe — a 40x4096 probe every
    TTL would bill the operator, and their failure mode is auth/quota, not our
    batch size."""
    probes = []

    def fake_score(query, passages):
        probes.append(list(passages))
        return [1.0] * len(passages)

    monkeypatch.setattr(rerank, "score", fake_score)
    monkeypatch.setenv("VOYAGE_API_KEY", "present")
    with pin_cfg("rerank", provider="voyage", model="voyage-rerank-2.5",
                 preflight_ceiling_ms="5000"), \
         pin_cfg("retrieval", rerank_candidate_n="40"):
        pf = rerank.preflight()
    assert pf["state"] == "ok"
    assert len(probes) == 1 and len(probes[0]) == 1


# ── 3. THE FALLTHROUGH MUST STAY VISIBLE ─────────────────────────────────────

def test_fallthrough_is_observable_more_than_once(monkeypatch, capsys):
    """Un-reranked results are a real degradation of search quality. Logging it
    once per process hides it forever in a long-lived service."""
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    _pin_localstack(monkeypatch)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local"):
        for _ in range(3):
            assert rerank.score("q", ["p"]) is None
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if "un-reranked" in ln]
    assert len(lines) >= 3
    assert rerank.fallthrough_state()["count"] == 3


def test_fallthrough_reason_is_on_status(monkeypatch):
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    _pin_localstack(monkeypatch)
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": False, "gpu_name": None, "vram_mb": None, "ram_mb": 32768,
        "source": "none", "tier": "MID"})
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local"):
        assert rerank.score("q", ["p"]) is None
        ft = rerank.status()["fallthrough"]
    assert ft["count"] >= 1
    # actionable, not just "something failed": the code AND the batch shape
    assert "500" in ft["reason"] and "1 passages" in ft["reason"]
    assert ft["provider"] == "localstack"


def test_preflight_probes_do_not_count_as_fallthroughs(monkeypatch):
    """The providers that STILL probe (metered/cpu) must not have their probe
    failure counted as a retrieve()-time degradation — it is already reported
    as the preflight state."""
    monkeypatch.setattr(rerank.requests, "post",
                        lambda *a, **k: FakeResp(500, {}))
    monkeypatch.setenv("VOYAGE_API_KEY", "present")
    with pin_cfg("rerank", provider="voyage", model="voyage-rerank-2.5"):
        assert rerank.preflight()["state"] == "failed"
    assert rerank.fallthrough_state()["count"] == 0


def test_a_real_failure_overrides_earlier_ok_evidence(monkeypatch):
    """status() may never keep claiming available:true off an old success while
    live searches fail: the NEWEST real outcome is the state."""
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) == [1.0]
        assert rerank.available() is True
        monkeypatch.setattr(rerank.requests, "post",
                            lambda *a, **k: FakeResp(500, {}))
        assert rerank.score("q", ["p"]) is None
        assert rerank.preflight()["state"] == "failed"
        assert rerank.available() is False
    assert calls == [1]                       # no probe ever scored a batch


def test_voice_gate_fallthrough_is_not_capability_evidence(monkeypatch):
    """D12: a voice session deliberately holding the retrieval group is the
    design working, not a sick reranker. It is counted and logged, but it must
    NOT flip capability to failed (that would lock rerank out for a whole TTL
    after every voice-heavy search)."""
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(local_stack, "_voice_depth", 1)
    calls = _record_posts(monkeypatch)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        assert rerank.score("q", ["p"]) is None
        assert rerank.fallthrough_state()["count"] == 1   # counted + logged
        assert rerank.evidence_state()["state"] == "unknown"   # but NOT failed
        assert rerank.available() is True
    assert calls == []                        # the dispatch never fired


# ── 4. STATUS TELLS THE TRUTH ABOUT THE SCORING PATH ─────────────────────────

def test_status_reports_the_base_url_actually_used_for_scoring(monkeypatch):
    """The payload showed base_url http://localhost:8091 (the legacy vLLM seam)
    while scoring resolved to the llama-swap front door. Report the URL the
    scorer actually posts to, and a reachability derived from THAT path."""
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": True, "gpu_name": "RTX", "vram_mb": 16380, "ram_mb": 32768,
        "source": "nvidia-smi", "tier": "HIGH"})
    monkeypatch.setattr(rerank, "score", lambda q, p: None)
    _pin_localstack(monkeypatch, healthy=True)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local"):
        body = rerank.status()
    assert body["scoring_base_url"] == "http://127.0.0.1:9098/v1/rerank"
    assert body["scoring_reachable"] is True      # the path scoring uses
    assert body["service_reachable"] is False     # legacy :8091 seam, unchanged
    assert body["base_url"] == rerank.DEFAULT_BASE_URL


def test_scoring_reachable_follows_the_stack_not_the_legacy_seam(monkeypatch):
    monkeypatch.setattr(rerank.hardware, "probe", lambda **k: {
        "gpu": True, "gpu_name": "RTX", "vram_mb": 16380, "ram_mb": 32768,
        "source": "nvidia-smi", "tier": "HIGH"})
    monkeypatch.setattr(rerank, "score", lambda q, p: None)
    _pin_localstack(monkeypatch, healthy=False)
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local"):
        body = rerank.status()
    assert body["scoring_reachable"] is False


def test_scoring_base_url_per_provider(monkeypatch):
    """Every provider reports where IT actually scores — never a URL it does
    not use."""
    _pin_localstack(monkeypatch, base="http://127.0.0.1:9098/v1")
    with pin_cfg("rerank", provider="localstack",
                 model="qwen3-reranker-8b-local"):
        assert rerank.scoring_base_url().endswith(":9098/v1/rerank")
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 model="qwen3-reranker-0.6b"):
        assert rerank.scoring_base_url() == "http://h:1/score"
    with pin_cfg("rerank", provider="cpu", model="qwen3-reranker-0.6b-cpu"):
        assert rerank.scoring_base_url() is None      # in-process, no URL
    with pin_cfg("rerank", provider="voyage", model="voyage-rerank-2.5"):
        assert "voyageai.com" in rerank.scoring_base_url()
    with pin_cfg("rerank", provider="null", model=None):
        assert rerank.scoring_base_url() is None


def test_real_rerank_outcomes_feed_the_local_stack_breaker(monkeypatch):
    """GET /local-models/status says "member failing" while the front door
    is up (audit O19-local-stack-05): 5xx and transport errors count, a 200
    closes it. Reporting only — the evidence regime still gates reranking."""
    from Orchestrator import local_stack
    _record_posts(monkeypatch, status=500)
    _pin_localstack(monkeypatch)
    with _localstack_cfg():
        for _ in range(local_stack.BREAKER_THRESHOLD):
            assert rerank.score("q", ["p"]) is None
        st = local_stack.breaker_state("rerank")
        assert st["state"] == "open" and st["last_error"] == "HTTP 500"

        def refused(url, json=None, timeout=None):
            raise rerank.requests.exceptions.ConnectionError("refused")
        monkeypatch.setattr(rerank.requests, "post", refused)
        assert rerank.score("q", ["p"]) is None
        assert local_stack.breaker_state("rerank")["consecutive_failures"] == \
            local_stack.BREAKER_THRESHOLD + 1

        _record_posts(monkeypatch, status=200)
        assert rerank.score("q", ["p"]) == [1.0]
        assert local_stack.breaker_state("rerank")["state"] == "closed"
