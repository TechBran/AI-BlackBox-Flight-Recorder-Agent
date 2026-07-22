"""M-B Task B1 — hardware-probe best-fit Whisper selection.

stt_stream_model()/stt_batch_model() resolve the on-box whisper ids LIVE from a
small WHISPER_FIT table keyed by the hardware probe's VRAM (mirroring rerank.py's
RERANK_MODELS + hardware.probe().vram_mb tiering), with a fresh-read sidecar
override and a fail-open fall back to today's flagship constants on probe failure.

Additive invariant: when the stack is off, no live STT path invokes the getters
(and therefore never touches the probe) — cloud STT is byte-for-byte unaffected.
"""
from Orchestrator import hardware, local_stack


# ── best-fit tiers by probed VRAM ────────────────────────────────────────────

def _fresh(monkeypatch, tmp_path):
    """Point the sidecar at an absent tmp file so the resolver is probe-driven."""
    monkeypatch.setattr(local_stack, "WHISPER_FIT_SIDECAR_PATH",
                        tmp_path / "whisper_fit.json")


def test_16gb_gpu_gets_the_flagship_pairing(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 2000 Ada", "vram_mb": 16380,
        "ram_mb": 64000, "source": "nvidia-smi", "tier": "HIGH"})
    assert local_stack.stt_stream_model() == "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert local_stack.stt_batch_model() == "Systran/faster-whisper-large-v3"


def test_8gb_gpu_drops_to_a_smaller_int8_tier(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 3070", "vram_mb": 8192,
        "ram_mb": 32000, "source": "nvidia-smi", "tier": "HIGH"})
    stream = local_stack.stt_stream_model()
    batch = local_stack.stt_batch_model()
    # smaller than the 16GB batch (not full large-v3); an int8 tier
    assert batch != "Systran/faster-whisper-large-v3"
    fit = local_stack.resolve_whisper_fit()
    assert fit["compute_type"] == "int8"
    assert fit["min_vram_mb"] == 8000
    # streaming stays a real faster-whisper repo id
    assert "whisper" in stream


def test_cpu_only_gets_int8_small_base(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": False, "gpu_name": None, "vram_mb": None,
        "ram_mb": 31000, "source": "none", "tier": "LOW"})
    stream = local_stack.stt_stream_model()
    batch = local_stack.stt_batch_model()
    # int8 small/base family — never the GPU flagship
    assert stream not in ("deepdml/faster-whisper-large-v3-turbo-ct2",)
    assert batch not in ("Systran/faster-whisper-large-v3",)
    assert any(sz in stream for sz in ("small", "base"))
    assert any(sz in batch for sz in ("small", "base"))
    assert local_stack.resolve_whisper_fit()["compute_type"] == "int8"


def test_first_matching_tier_wins_high_before_low(monkeypatch, tmp_path):
    """A 24GB card still resolves the 16GB flagship tier (first match, ordered)."""
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 4090", "vram_mb": 24564,
        "ram_mb": 128000, "source": "nvidia-smi", "tier": "HIGH"})
    assert local_stack.resolve_whisper_fit()["min_vram_mb"] == 16000


# ── fail-open fallbacks ──────────────────────────────────────────────────────

def test_probe_error_falls_back_to_today_constants(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    def _boom(*a, **k):
        raise RuntimeError("nvidia-smi exploded")
    monkeypatch.setattr(hardware, "probe", _boom)
    assert local_stack.resolve_whisper_fit() is None
    assert local_stack.stt_stream_model() == local_stack.ONBOX_STT_STREAM_MODEL
    assert local_stack.stt_batch_model() == local_stack.ONBOX_STT_BATCH_MODEL


def test_gpu_with_unverifiable_vram_falls_back_to_flagship_constants(monkeypatch, tmp_path):
    """lspci-discovered NVIDIA card (vram_mb None) → today's flagship constants
    (matches hardware.derive_tier's 'unverifiable VRAM => HIGH' stance)."""
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "NVIDIA (lspci)", "vram_mb": None,
        "ram_mb": 64000, "source": "lspci", "tier": "HIGH"})
    assert local_stack.resolve_whisper_fit() is None
    assert local_stack.stt_stream_model() == local_stack.ONBOX_STT_STREAM_MODEL
    assert local_stack.stt_batch_model() == local_stack.ONBOX_STT_BATCH_MODEL


# ── fresh-read sidecar override (future wizard) ──────────────────────────────

def test_sidecar_override_wins_and_is_fresh_read(monkeypatch, tmp_path):
    side = tmp_path / "whisper_fit.json"
    monkeypatch.setattr(local_stack, "WHISPER_FIT_SIDECAR_PATH", side)
    # a 16GB GPU would otherwise pick the flagship pair
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 2000 Ada", "vram_mb": 16380,
        "ram_mb": 64000, "source": "nvidia-smi", "tier": "HIGH"})
    side.write_text('{"stream": "custom/stream-model", "batch": "custom/batch-model"}',
                    encoding="utf-8")
    assert local_stack.stt_stream_model() == "custom/stream-model"
    assert local_stack.stt_batch_model() == "custom/batch-model"
    # fresh read: editing the sidecar takes effect with no restart / no cache bust
    side.write_text('{"stream": "custom/stream-2", "batch": "custom/batch-2"}',
                    encoding="utf-8")
    assert local_stack.stt_stream_model() == "custom/stream-2"


def test_corrupt_sidecar_is_ignored_falls_through_to_probe(monkeypatch, tmp_path):
    side = tmp_path / "whisper_fit.json"
    side.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(local_stack, "WHISPER_FIT_SIDECAR_PATH", side)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 2000 Ada", "vram_mb": 16380,
        "ram_mb": 64000, "source": "nvidia-smi", "tier": "HIGH"})
    assert local_stack.read_whisper_fit_sidecar() == {}
    assert local_stack.stt_stream_model() == "deepdml/faster-whisper-large-v3-turbo-ct2"


def test_partial_sidecar_only_overrides_present_kind(monkeypatch, tmp_path):
    side = tmp_path / "whisper_fit.json"
    side.write_text('{"stream": "custom/stream-only"}', encoding="utf-8")
    monkeypatch.setattr(local_stack, "WHISPER_FIT_SIDECAR_PATH", side)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": True, "gpu_name": "RTX 2000 Ada", "vram_mb": 16380,
        "ram_mb": 64000, "source": "nvidia-smi", "tier": "HIGH"})
    assert local_stack.stt_stream_model() == "custom/stream-only"
    # batch has no override → probe best-fit (flagship)
    assert local_stack.stt_batch_model() == "Systran/faster-whisper-large-v3"


# ── additive invariant: stack OFF → no live path calls the getters/probe ─────

def test_stack_off_stt_catalog_never_touches_the_probe(monkeypatch, tmp_path):
    """With no [local_models] section the stack is off: build_stt_catalog() must
    NOT append an onbox provider and therefore never invoke the whisper-fit
    getters (which are the only callers of the hardware probe on the STT path).
    Cloud STT providers remain present + unaffected."""
    # config.ini WITHOUT a [local_models] section → master_enabled() False
    cfg = tmp_path / "config.ini"
    cfg.write_text("[server]\nport = 9091\n", encoding="utf-8")
    monkeypatch.setattr(local_stack, "CONFIG_PATH", cfg)

    calls = {"probe": 0}
    real_probe = hardware.probe
    def _spy(*a, **k):
        calls["probe"] += 1
        return real_probe(*a, **k)
    monkeypatch.setattr(hardware, "probe", _spy)

    from Orchestrator.stt.catalog import build_stt_catalog
    catalog = build_stt_catalog()
    ids = [p["id"] for p in catalog]

    assert "onbox" not in ids                       # inert when off
    assert {"openai", "google", "elevenlabs"} <= set(ids)  # cloud unaffected
    assert calls["probe"] == 0                       # getters never ran → no probe


def test_getters_are_pure_no_side_effects_when_called_directly(monkeypatch, tmp_path):
    """The getters are safe to call regardless of stack state (they only read a
    fresh sidecar + the cached probe); they never write, never raise."""
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {
        "gpu": False, "gpu_name": None, "vram_mb": None,
        "ram_mb": 31000, "source": "none", "tier": "LOW"})
    assert isinstance(local_stack.stt_stream_model(), str)
    assert isinstance(local_stack.stt_batch_model(), str)
    assert not (tmp_path / "whisper_fit.json").exists()  # no write
