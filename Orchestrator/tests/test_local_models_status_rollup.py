"""status_rollup._derive_local_models (M8): probe-free hub summary for the
on-box stack, driven purely by the persisted [local_models] flags + the
STT_PROVIDER env snapshot passed in by _collect_status_inputs."""
from Orchestrator.onboarding import status_rollup as sr


def _lm(**kw):
    base = {"enabled": False,
            "capabilities": {"stt": False, "tts": False,
                             "embeddings": False, "rerank": False},
            "stt_provider": ""}
    base.update(kw)
    return base


def test_disabled_is_optional():
    st, summary, items, atts = sr._derive_local_models(_lm())
    assert st == sr.OPTIONAL
    assert atts == []


def test_stt_onbox_via_env_counts_even_when_flag_missing():
    st, summary, items, atts = sr._derive_local_models(_lm(stt_provider="onbox"))
    assert st == sr.READY
    assert "1" in summary


def test_multiple_capabilities_ready():
    lm = _lm(enabled=True,
             capabilities={"stt": True, "tts": True,
                           "embeddings": True, "rerank": False},
             stt_provider="onbox")
    st, summary, items, atts = sr._derive_local_models(lm)
    assert st == sr.READY
    assert "3" in summary  # stt + tts + embeddings (stt not double-counted)


def test_build_status_accepts_local_models_kwarg():
    # build_status must thread the new kwarg (regression guard against a
    # missing dispatch branch falling back to _derive_default silently).
    out = sr.build_status(
        env={}, state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
        embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
        cli={"providers": {}}, web_search={"enabled": [], "providers": {}, "default": ""},
        image={"enabled": [], "providers": {}, "default": ""}, paired=[], operators=["A"],
        restart={"needs_restart": False}, local_models=_lm(enabled=True,
            capabilities={"stt": True, "tts": False, "embeddings": False, "rerank": False}),
    )
    sec = next(s for s in out["sections"] if s["key"] == "local_models")
    assert sec["state"] == sr.READY
