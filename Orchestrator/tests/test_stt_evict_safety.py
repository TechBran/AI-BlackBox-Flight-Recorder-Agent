"""Unit tests for the G6 streaming-STT eviction-safety probe's PURE decision
math (no GPU, no WS). These cover the cut-off-detection logic that the live
MS02 run exercises against real audio — specifically the empty-gaps/None and
total-cutoff cases that a gap-only metric silently green-lights."""
from diagnostics.localstack.stt_evict_safety import (
    partial_gaps, max_gap, normalize_transcript, decide,
    FRAME_MS, FRAME_S)


def _run(n_partials, gap, transcript="hello world", stt_done=True):
    return {"inject": True, "n_partials": n_partials,
            "max_partial_gap_s": gap, "transcript": transcript,
            "stt_done": stt_done}


# --- gap math -------------------------------------------------------------

def test_partial_gaps_basic():
    assert partial_gaps([1.0, 1.5, 2.5]) == [0.5, 1.0]


def test_max_gap_none_when_fewer_than_two_partials():
    # 0 or 1 partial -> no measurable gap -> None (a SIGNAL, never 0.0).
    assert max_gap([]) is None
    assert max_gap([3.14]) is None


def test_max_gap_value():
    assert max_gap([0.0, 0.1, 0.9]) == 0.8


def test_frame_pacing_locked_to_frame_ms():
    # The send-loop sleep must be derived from the single frame-duration
    # constant, not an independent literal.
    assert FRAME_S == FRAME_MS / 1000.0 == 0.1


# --- transcript normalization --------------------------------------------

def test_normalize_transcript_case_and_whitespace():
    assert normalize_transcript("  Hello   WORLD ") == "hello world"
    assert normalize_transcript(None) == ""


# --- decision logic -------------------------------------------------------

def test_pass_when_control_and_injected_match():
    ctl = _run(10, 0.2)
    inj = _run(10, 0.3)
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is True
    assert v["audio_cut_off"] is False
    assert v["cut_off_reasons"] == []


def test_total_cutoff_zero_partials_fails_not_passes():
    # THE hole: catastrophic cut-off -> 0 partials -> None gap. The old code
    # coerced None to 0.0, computed 0-0<=tol -> cut_off False, and with a
    # stt_done event returned PASS on total silence. This must FAIL.
    ctl = _run(10, 0.2)
    inj = _run(0, None, transcript="", stt_done=True)
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is False
    assert v["audio_cut_off"] is True
    assert "insufficient_partials" in v["cut_off_reasons"]


def test_single_partial_none_gap_fails():
    ctl = _run(10, 0.2)
    inj = _run(1, None, transcript="hello", stt_done=True)
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is False
    assert "insufficient_partials" in v["cut_off_reasons"]


def test_trailing_cutoff_shows_as_partial_count_drop():
    # A trailing truncation halves the partials without widening any gap; the
    # gap-only metric would miss it, the count check catches it.
    ctl = _run(20, 0.2)
    inj = _run(6, 0.25, transcript="hello world")  # <50% retained
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is False
    assert "partial_count_drop" in v["cut_off_reasons"]


def test_inter_partial_stall_over_tolerance_fails():
    ctl = _run(10, 0.2)
    inj = _run(10, 2.0)  # +1.8s stall > 1.0 tolerance
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is False
    assert "inter_partial_stall" in v["cut_off_reasons"]
    assert v["extra_gap_s"] == 1.8


def test_within_tolerance_stall_passes():
    ctl = _run(10, 0.2)
    inj = _run(10, 1.0)  # +0.8s <= 1.0 tolerance
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["audio_cut_off"] is False
    assert v["pass"] is True


def test_transcript_mismatch_fails_even_when_clean():
    # No cut-off, stt_done true, but transcript diverged from control -> the
    # docstring's condition (d) must actually fail the run.
    ctl = _run(10, 0.2, transcript="the quick brown fox")
    inj = _run(10, 0.3, transcript="totally different words")
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["transcript_match"] is False
    assert v["pass"] is False


def test_no_stt_done_fails():
    # A never-terminating stream (TimeoutError path) leaves stt_done False.
    ctl = _run(10, 0.2)
    inj = _run(10, 0.3, stt_done=False)
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["pass"] is False


def test_extra_gap_none_when_injected_gap_none():
    ctl = _run(10, 0.2)
    inj = _run(0, None, transcript="")
    v = decide(ctl, inj, gap_tolerance_s=1.0)
    assert v["extra_gap_s"] is None
