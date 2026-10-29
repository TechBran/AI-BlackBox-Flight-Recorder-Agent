"""local_image is recognized at the image-tool dispatch + media-classification sites."""


def test_local_image_in_tool_providers():
    # Covers every dispatch site that keys off `name in IMAGE_TOOL_PROVIDERS`
    # (the shared providers/tool_dispatch.py, which the chat loops and the
    # Anthropic CU driver use, and the 3 voice routes).
    from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
    assert IMAGE_TOOL_PROVIDERS["local_image"] == "local"


def test_local_image_media_kind():
    # The chat loop records the generation task for the UI placeholder animation
    # via this separate tool->media-kind map.
    from Orchestrator.routes.chat_routes import _media_kind
    assert _media_kind("local_image") == "image"
