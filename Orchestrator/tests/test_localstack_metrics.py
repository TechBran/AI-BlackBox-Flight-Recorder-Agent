import struct
import pytest
from diagnostics.localstack.metrics import (
    parse_nvidia_smi_used_mib, wav_duration_seconds, parse_wav_header, rtf,
    summarize_latencies)


def _wav(sample_rate=24000, channels=1, bits=16, seconds=1.0, junk_before_fmt=False):
    n = int(sample_rate * seconds)
    data = (b"\x00\x00") * n * channels
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
    # A non-canonical layout: a JUNK chunk sitting BEFORE the fmt chunk. A fixed
    # offset-24 read of sample_rate breaks here; a chunk walk does not.
    junk = (b"JUNK" + struct.pack("<I", 8) + b"\x00" * 8) if junk_before_fmt else b""
    body = (junk + b"fmt " + struct.pack("<I", len(fmt)) + fmt +
            b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def test_parse_used_mib_first_line():
    assert parse_nvidia_smi_used_mib("10278\n") == 10278


def test_parse_used_mib_skips_blank_takes_first():
    assert parse_nvidia_smi_used_mib("\n  \n11842\n3284\n") == 11842


def test_parse_used_mib_strips_stray_column():
    assert parse_nvidia_smi_used_mib("11800, 16380\n") == 11800


def test_parse_used_mib_raises_on_empty():
    with pytest.raises(ValueError):
        parse_nvidia_smi_used_mib("\n   \n")


def test_wav_duration_one_second():
    assert abs(wav_duration_seconds(_wav(seconds=1.0)) - 1.0) < 1e-6


def test_wav_duration_half_second_48k_stereo():
    assert abs(wav_duration_seconds(_wav(48000, 2, 16, 0.5)) - 0.5) < 1e-6


def test_wav_duration_rejects_garbage():
    with pytest.raises(ValueError):
        wav_duration_seconds(b"not a wav at all")


def test_parse_wav_header_reads_real_sample_rate():
    hdr = parse_wav_header(_wav(48000, 2, 16, 0.5))
    assert hdr["sample_rate"] == 48000
    assert hdr["channels"] == 2
    assert abs(hdr["duration_seconds"] - 0.5) < 1e-6


def test_parse_wav_header_robust_to_chunk_before_fmt():
    # fmt is NOT the first chunk here; a hardcoded offset-24 read would return
    # garbage. The chunk walk must still recover the true sample rate.
    hdr = parse_wav_header(_wav(16000, 1, 16, 0.25, junk_before_fmt=True))
    assert hdr["sample_rate"] == 16000
    assert abs(hdr["duration_seconds"] - 0.25) < 1e-6


def test_parse_wav_header_rejects_garbage():
    with pytest.raises(ValueError):
        parse_wav_header(b"not a wav at all")


def test_rtf_basic():
    assert rtf(0.45, 1.0) == 0.45


def test_rtf_rejects_zero_audio():
    with pytest.raises(ValueError):
        rtf(1.0, 0.0)


def test_summarize_latencies():
    assert summarize_latencies([1.0, 3.0, 2.0]) == {
        "n": 3, "min_s": 1.0, "median_s": 2.0, "max_s": 3.0,
        "p90_s": 2.8, "p95_s": 2.9, "p99_s": 2.98}


def test_summarize_latencies_unsorted_input():
    # Percentiles must not depend on input order.
    assert summarize_latencies([3.0, 1.0, 2.0]) == summarize_latencies([1.0, 2.0, 3.0])


def test_summarize_latencies_single_sample():
    out = summarize_latencies([0.5])
    assert out["p90_s"] == out["p95_s"] == out["p99_s"] == 0.5


def test_summarize_latencies_empty():
    with pytest.raises(ValueError):
        summarize_latencies([])
