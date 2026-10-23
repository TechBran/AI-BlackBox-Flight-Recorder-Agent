"""Pure-Python tests for audio_io. MicStream/SpeakerStream require real
ALSA devices and are exercised by the Task 11 integration suite — not
here, by design, so the unit suite stays fast and CI-portable.
"""
import struct

from ugv_tools_api.supervisor.audio_io import make_chime


def test_chime_is_pcm_s16le_and_not_empty():
    data = make_chime(duration_s=0.1, freq_hz=880.0, sample_rate=24000)
    assert isinstance(data, bytes)
    assert len(data) == int(0.1 * 24000) * 2  # 2 bytes per sample


def test_chime_fades_in_and_out():
    """First and last sample should be near-zero due to 20ms fade."""
    data = make_chime(duration_s=0.1, freq_hz=880.0, sample_rate=24000)
    samples = struct.unpack(f"<{len(data)//2}h", data)
    assert abs(samples[0]) < 100, f"leading sample {samples[0]} not faded"
    assert abs(samples[-1]) < 100, f"trailing sample {samples[-1]} not faded"
    # Past the 20 ms fade-in, the waveform should be at full amplitude.
    # Don't pick a single midpoint sample — at some freq/rate combos the
    # midpoint lands on a zero crossing (e.g. 880 Hz * 0.05s = 44 full
    # cycles exactly). Instead, check the peak in the middle 60% exceeds
    # the fade threshold.
    n = len(samples)
    mid_region = samples[n // 5 : 4 * n // 5]
    peak_mid = max(abs(s) for s in mid_region)
    assert peak_mid > 2000, f"middle-region peak {peak_mid} suspiciously quiet"


def test_chime_volume_clamp_is_reasonable():
    """default volume=0.25 should produce peak amplitude near 0.25 * 32767."""
    data = make_chime(duration_s=0.5, freq_hz=880.0, sample_rate=24000)
    samples = struct.unpack(f"<{len(data)//2}h", data)
    peak = max(abs(s) for s in samples)
    # 0.25 * 32767 = 8191. Allow slack for fade.
    assert 7000 < peak < 8500, f"unexpected peak amplitude {peak}"
