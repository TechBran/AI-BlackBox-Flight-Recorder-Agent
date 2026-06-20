from Orchestrator.toolvault import registry

NEW = ["gemini_image", "openai_image", "grok_image"]


def test_three_image_tools_load():
    names = {t["name"] for t in registry.load_canonical()}
    for n in NEW:
        assert n in names, f"missing {n}"
    assert "generate_image" not in names


def test_image_tools_feature_and_gate():
    by = {t["name"]: t for t in registry.load_canonical()}
    for n in NEW:
        assert by[n]["x-availability"]["feature"] == "image"
    assert by["openai_image"]["x-availability"]["requires_env"] == ["OPENAI_API_KEY"]
    assert by["grok_image"]["x-availability"]["provider"] == "grok"


def test_grok_image_no_resolution_param():
    by = {t["name"]: t for t in registry.load_canonical()}
    assert "resolution" not in by["grok_image"]["parameters"]["properties"]
