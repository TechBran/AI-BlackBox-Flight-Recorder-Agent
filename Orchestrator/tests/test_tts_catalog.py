"""Single-source-of-truth TTS voice catalog (2026-05-31)."""
from Orchestrator.config import (
    build_tts_catalog,
    GEMINI_TTS_VOICE_DESCRIPTIONS,
    GEMINI_LIVE_VOICES,
    OPENAI_TTS_VOICES,
)

def test_three_groups_in_order():
    assert [g["id"] for g in build_tts_catalog()] == ["openai", "gemini-flash", "gemini-pro"]

def test_group_counts():
    g = {x["id"]: x for x in build_tts_catalog()}
    assert len(g["openai"]["voices"]) == 11
    assert len(g["gemini-flash"]["voices"]) == 30
    assert len(g["gemini-pro"]["voices"]) == 30

def test_ids_prefixed_and_fully_described():
    for grp in build_tts_catalog():
        for v in grp["voices"]:
            assert v["id"].startswith(grp["id"] + ":")
            assert v["name"] and v["description"]

def test_gemini_descriptions_cover_all_30_live_names():
    assert set(GEMINI_TTS_VOICE_DESCRIPTIONS) == set(GEMINI_LIVE_VOICES)

def test_flash_and_pro_share_names_differ_by_prefix():
    g = {x["id"]: x for x in build_tts_catalog()}
    flash = [v["name"] for v in g["gemini-flash"]["voices"]]
    pro = [v["name"] for v in g["gemini-pro"]["voices"]]
    assert flash == pro == GEMINI_LIVE_VOICES
    assert g["gemini-flash"]["voices"][0]["id"] == "gemini-flash:Zephyr"
    assert g["gemini-pro"]["voices"][0]["id"] == "gemini-pro:Zephyr"
