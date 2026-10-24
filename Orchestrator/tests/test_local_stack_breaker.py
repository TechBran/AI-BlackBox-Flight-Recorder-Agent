"""Per-capability circuit breaker + gpu_ready() in Orchestrator/local_stack.py
(2026-08-11 plan Track B Task 6, audit W11 / O19-local-stack-05).

is_healthy() answers "is the llama-swap PROCESS up" and must stay that way (the
anti-flap invariant: a group swap never flips routing). It cannot see a member
that 500s every request — the Aug outage served TTS 500s behind a green
/health for four days. The breaker is the second, additive signal: it is fed
by REAL request outcomes, opens after BREAKER_THRESHOLD consecutive capability
failures (or one fatal one, e.g. "no CUDA"), reports the capability
unavailable for a cooldown with one loud log line, then lets one request probe
it (half-open). The clock is the module's _breaker_clock seam.
"""
import logging

import httpx
import pytest

from Orchestrator import hardware, local_stack


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(local_stack, "_breaker_clock", c)
    local_stack.reset_breakers()
    yield c
    local_stack.reset_breakers()


def _fail(cap, n, error="HTTP 500: Internal Server Error", **kw):
    for _ in range(n):
        local_stack.report_failure(cap, error, **kw)


# ── state machine ─────────────────────────────────────────────────────────────

def test_starts_closed_for_every_capability(clock):
    for cap in local_stack.CAPABILITIES:
        st = local_stack.breaker_state(cap)
        assert st["state"] == "closed" and st["consecutive_failures"] == 0
        assert local_stack.capability_ok(cap) is True


def test_opens_on_the_threshold_th_consecutive_failure(clock):
    _fail("tts", local_stack.BREAKER_THRESHOLD - 1)
    assert local_stack.capability_ok("tts") is True
    _fail("tts", 1, error="HTTP 500: CUDA error: no kernel image")
    st = local_stack.breaker_state("tts")
    assert st["state"] == "open"
    assert st["last_error"] == "HTTP 500: CUDA error: no kernel image"
    assert st["retry_in_s"] == int(local_stack.BREAKER_COOLDOWN_S)
    assert local_stack.capability_ok("tts") is False
    # per capability: STT is unaffected
    assert local_stack.capability_ok("stt") is True


def test_a_success_resets_the_consecutive_count(clock):
    _fail("stt", local_stack.BREAKER_THRESHOLD - 1)
    local_stack.report_success("stt")
    _fail("stt", local_stack.BREAKER_THRESHOLD - 1)
    assert local_stack.breaker_state("stt")["state"] == "closed"


def test_one_fatal_failure_opens_immediately(clock):
    local_stack.report_failure("tts", "qwen-tts needs a CUDA GPU", fatal=True)
    assert local_stack.breaker_state("tts")["state"] == "open"


def test_cooldown_expiry_half_opens_then_a_success_closes(clock):
    _fail("tts", local_stack.BREAKER_THRESHOLD)
    clock.now += local_stack.BREAKER_COOLDOWN_S
    assert local_stack.breaker_state("tts")["state"] == "half_open"
    assert local_stack.capability_ok("tts") is True   # one request may probe it
    local_stack.report_success("tts")
    st = local_stack.breaker_state("tts")
    assert st["state"] == "closed" and st["consecutive_failures"] == 0


def test_a_failed_probe_reopens_at_twice_the_cooldown_capped(clock):
    _fail("tts", local_stack.BREAKER_THRESHOLD)
    cooldown = local_stack.BREAKER_COOLDOWN_S
    for _ in range(12):
        clock.now += cooldown
        assert local_stack.breaker_state("tts")["state"] == "half_open"
        _fail("tts", 1)
        cooldown = min(cooldown * 2, local_stack.BREAKER_COOLDOWN_MAX_S)
        st = local_stack.breaker_state("tts")
        assert st["state"] == "open" and st["retry_in_s"] == int(cooldown)
    assert cooldown == local_stack.BREAKER_COOLDOWN_MAX_S


def test_a_recovery_resets_the_cooldown(clock):
    _fail("tts", local_stack.BREAKER_THRESHOLD)
    clock.now += local_stack.BREAKER_COOLDOWN_S
    _fail("tts", 1)                                   # reopened at 2x
    clock.now += 2 * local_stack.BREAKER_COOLDOWN_S
    local_stack.report_success("tts")
    _fail("tts", local_stack.BREAKER_THRESHOLD)
    assert local_stack.breaker_state("tts")["retry_in_s"] == int(local_stack.BREAKER_COOLDOWN_S)


def test_one_loud_line_per_trip_and_one_on_recovery(clock, caplog):
    caplog.set_level(logging.WARNING, logger=local_stack.logger.name)
    _fail("stt", local_stack.BREAKER_THRESHOLD + 5, error="HTTP 502: upstream dead")
    trips = [r for r in caplog.records if "degraded" in r.getMessage()]
    assert len(trips) == 1
    msg = trips[0].getMessage()
    assert msg.startswith("[LOCAL STACK] stt degraded")
    assert "HTTP 502: upstream dead" in msg and "60s" in msg
    assert trips[0].levelno == logging.ERROR
    clock.now += local_stack.BREAKER_COOLDOWN_S
    local_stack.report_success("stt")
    assert any("[LOCAL STACK] stt recovered" in r.getMessage() for r in caplog.records)


def test_success_on_a_healthy_capability_is_silent(clock, caplog):
    caplog.set_level(logging.DEBUG, logger=local_stack.logger.name)
    local_stack.report_success("tts")
    assert not caplog.records


def test_unknown_capability_is_a_programming_error(clock):
    with pytest.raises(ValueError):
        local_stack.report_failure("vision", "x")
    with pytest.raises(ValueError):
        local_stack.breaker_state("vision")


def test_error_text_is_bounded(clock):
    local_stack.report_failure("tts", "x" * 5000, fatal=True)
    assert len(local_stack.breaker_state("tts")["last_error"]) <= 300


# ── gpu_ready(): additive, fail-open ──────────────────────────────────────────

_HW = {"gpu": True, "gpu_name": "RTX 2000 Ada", "vram_mb": 16380, "ram_mb": 128000,
       "source": "nvidia-smi", "tier": "HIGH", "driver_ok": True, "gpu_reason": None}


@pytest.fixture
def installed_cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text("[local_models]\nenabled = true\ntts = true\nstt = true\n",
                    encoding="utf-8")
    monkeypatch.setattr(local_stack, "CONFIG_PATH", path)
    monkeypatch.setattr(local_stack, "_transport", httpx.MockTransport(
        lambda request: httpx.Response(200, text="OK")))
    return path


def test_gpu_ready_false_when_driver_dead_but_is_healthy_unchanged(monkeypatch, installed_cfg):
    reason = "NVIDIA driver not responding: RTX 2000 Ada is on the PCI bus"
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(
        _HW, vram_mb=None, source="lspci", driver_ok=False, gpu_reason=reason))
    assert local_stack.is_healthy() is True          # the anti-flap invariant holds
    assert local_stack.gpu_ready() == {"ok": False, "reason": reason}


@pytest.mark.parametrize("driver_ok", [True, None])
def test_gpu_ready_fails_open(monkeypatch, driver_ok):
    """A healthy GPU box and every box with nothing to judge (CPU-only, card
    without a driver) disable nothing."""
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(_HW, driver_ok=driver_ok))
    assert local_stack.gpu_ready() == {"ok": True, "reason": None}


def test_gpu_ready_fails_open_on_a_probe_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(hardware, "probe", boom)
    assert local_stack.gpu_ready() == {"ok": True, "reason": None}


# ── capability_problem(): what the gates read ─────────────────────────────────

def test_capability_problem_none_when_all_is_well(clock, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(_HW))
    for cap in local_stack.CAPABILITIES:
        assert local_stack.capability_problem(cap) is None


def test_tts_needs_the_gpu_but_stt_does_not(clock, monkeypatch):
    """D4: there is no CPU path for Qwen TTS, so a dead driver makes on-box TTS
    unavailable. STT/embeddings/rerank still work on CPU (slowly) — they stay
    available; the status payload marks them degraded instead."""
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(
        _HW, driver_ok=False, gpu_reason="NVIDIA driver not responding"))
    assert "NVIDIA driver not responding" in local_stack.capability_problem("tts")
    for cap in ("stt", "embeddings", "rerank"):
        assert local_stack.capability_problem(cap) is None


def test_capability_problem_names_the_open_breaker(clock, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(_HW))
    _fail("stt", local_stack.BREAKER_THRESHOLD, error="HTTP 500: whisper load failed")
    problem = local_stack.capability_problem("stt")
    assert "HTTP 500: whisper load failed" in problem and "60s" in problem


def test_should_route_onbox_reads_the_breaker(clock, monkeypatch, installed_cfg):
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(_HW))
    assert local_stack.should_route_onbox("stt") is True
    _fail("stt", local_stack.BREAKER_THRESHOLD)
    assert local_stack.should_route_onbox("stt") is False
    assert local_stack.is_healthy() is True          # liveness is a separate signal


# ── suite isolation: the host's GPU never decides a test ─────────────────────

def test_a_test_that_chose_no_hardware_never_sees_the_hosts_dead_driver(monkeypatch):
    """The TTS gate reads the real hardware probe, so on a host whose NVIDIA
    driver is dead (MS02 during an outage, exactly when these suites get run)
    every TTS test that leaves hardware alone saw on-box TTS unavailable. The
    root conftest pins the GPU signal for such tests; a test that patches
    hardware.probe still gets the state it chose (the tests above)."""
    monkeypatch.setattr(hardware, "_cache", None)
    monkeypatch.setattr(hardware, "_probe_gpu", lambda: (
        True, "RTX 2000 Ada", None, "lspci", False, "NVIDIA driver not responding"))
    assert hardware.probe()["driver_ok"] is False        # this "host" is dead
    assert local_stack.gpu_ready() == {"ok": True, "reason": None}
    assert local_stack.capability_problem("tts") is None
