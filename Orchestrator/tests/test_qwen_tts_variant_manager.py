"""Control-logic tests for the variant manager — a fake backend records call
order (free/empty_cache/load/synth) so we prove FREE-BEFORE-LOAD and the
single-flight lock WITHOUT torch/CUDA. The real model never loads."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import asyncio

import pytest

from qwen_tts_server.variant_manager import VariantManager, VramError


class FakeBackend:
    def __init__(self, free_mb=8000):
        self.events = []
        self.free_mb = free_mb

    def load(self, variant, model_dir):
        self.events.append(("load", variant))
        return {"variant": variant, "sr": 22050}

    def free(self, handle):
        self.events.append(("free", handle["variant"]))

    def empty_cache(self):
        self.events.append(("empty_cache", None))

    def free_vram_mb(self):
        return self.free_mb

    def sample_rate(self, handle):
        return handle["sr"]

    def synth(self, handle, variant, text, *, preset=None, ref_audio=None,
              ref_text=None, design_params=None, language=None):
        # Signature mirrors TorchQwenBackend.synth (G3 rewrite: variant-aware +
        # ref_text/language threaded through by the manager).
        self.events.append(("synth", handle["variant"]))
        return (b"\x00\x01" * 100, handle["sr"])

    def synth_stream(self, handle, variant, text, *, preset=None, ref_audio=None,
                     ref_text=None, design_params=None, language=None):
        self.events.append(("synth_stream", handle["variant"]))
        yield (b"\x00\x01" * 100, handle["sr"])


def _mgr(be, tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_MODEL_DIR", str(tmp_path))
    return VariantManager(backend=be, min_free_mb=5000)


def test_first_load_reclaims_then_loads(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)
    pcm, sr = asyncio.run(mgr.synthesize_full("custom_voice", "hi", preset="Vivian"))
    assert sr == 22050 and pcm
    assert be.events == [("empty_cache", None), ("load", "custom_voice"), ("synth", "custom_voice")]


def test_variant_transition_frees_before_load(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await mgr.synthesize_full("custom_voice", "hi", preset="Vivian")
        be.events.clear()
        await mgr.synthesize_full("base", "hi", ref_audio="/x.wav")

    asyncio.run(scenario())
    # free(old) MUST precede load(new) — the whole point of FREE-BEFORE-LOAD.
    assert be.events == [
        ("free", "custom_voice"), ("empty_cache", None),
        ("load", "base"), ("synth", "base"),
    ]


def test_same_variant_no_reload(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await mgr.synthesize_full("custom_voice", "a", preset="Vivian")
        be.events.clear()
        await mgr.synthesize_full("custom_voice", "b", preset="Serena")

    asyncio.run(scenario())
    assert be.events == [("synth", "custom_voice")]  # no free/load


def test_low_vram_raises(tmp_path, monkeypatch):
    be = FakeBackend(free_mb=1000)  # below the 5000 floor
    mgr = _mgr(be, tmp_path, monkeypatch)
    with pytest.raises(VramError):
        asyncio.run(mgr.synthesize_full("custom_voice", "hi", preset="Vivian"))


def test_unknown_variant_raises(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(mgr.synthesize_full("nope", "hi"))


def test_lock_serializes_concurrent_synths(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await asyncio.gather(
            mgr.synthesize_full("custom_voice", "a", preset="Vivian"),
            mgr.synthesize_full("base", "b", ref_audio="/x.wav"),
        )

    asyncio.run(scenario())
    # Serialized: every ("load", X) is immediately followed by ("synth", X) with
    # no interleaved load/free from the other task.
    for i, ev in enumerate(be.events):
        if ev[0] == "load":
            assert be.events[i + 1] == ("synth", ev[1])
