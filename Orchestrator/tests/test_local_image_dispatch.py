"""local_image is recognized at the image-tool dispatch + media-classification sites."""


def test_local_image_in_tool_providers():
    # Covers all 11 dispatch sites that key off `name in IMAGE_TOOL_PROVIDERS`
    # (chat_routes x7, the 3 voice routes, driver_anthropic).
    from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
    assert IMAGE_TOOL_PROVIDERS["local_image"] == "local"


def test_local_image_media_kind():
    # The chat loop records the generation task for the UI placeholder animation
    # via this separate tool->media-kind map.
    from Orchestrator.routes.chat_routes import _media_kind
    assert _media_kind("local_image") == "image"
