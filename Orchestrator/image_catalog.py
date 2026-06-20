"""Image-generation param catalog -- the single source of truth (SoT).

Drives the provider-aware image UIs (Portal + Android). For every enabled image
provider, advertises ONLY the params that actually flow end-to-end:

    tool schema name  ==  GenIn field  ==  image_options key  ==  adapter read

If a param is in this spec it MUST reach the provider API; the coherence test in
test_image_catalog.py locks that contract so future drift fails CI.

NOTE: ``reference_images`` (image-to-image) is deliberately OMITTED from the v1
catalog -- there is a pre-existing name/shape mismatch (gemini snake_case URL
strings vs GenIn ``referenceImages`` camelCase object list). That is a deferred
design follow-up; the gemini tool schema keeps the param for the model, but the
UI catalog does not advertise it in v1.
"""

# Per-provider param spec. MUST match: the tool schema param names (Task 3),
# the GenIn fields, the image_options keys, and what the adapter reads.
IMAGE_PROVIDER_SPECS = {
    "gemini": {"label": "Gemini Nano Banana", "params": [
        {"name": "aspectRatio", "type": "enum", "options": ["1:1", "16:9", "9:16", "4:3", "3:4"], "default": "16:9"},
        {"name": "resolution", "type": "enum", "options": ["1K", "2K"], "default": "1K"},
        {"name": "numberOfImages", "type": "int", "min": 1, "max": 4, "default": 1}]},
    "openai": {"label": "OpenAI (gpt-image)", "params": [
        {"name": "size", "type": "enum", "options": ["1024x1024", "1536x1024", "1024x1536", "auto"], "default": "1024x1024"},
        {"name": "quality", "type": "enum", "options": ["low", "medium", "high"], "default": "high"},
        {"name": "numberOfImages", "type": "int", "min": 1, "max": 4, "default": 1}]},
    "grok": {"label": "Grok image", "params": [
        {"name": "aspectRatio", "type": "enum", "options": ["1:1", "16:9", "9:16"], "default": "16:9"},
        {"name": "numberOfImages", "type": "int", "min": 1, "max": 4, "default": 1}]},
}


def build_image_catalog() -> list:
    """Enabled image providers + their param schema (the SoT both UIs hydrate)."""
    from Orchestrator.toolvault.availability import enabled_providers, _read_env
    enabled = enabled_providers("image")
    default = (_read_env().get("IMAGE_DEFAULT") or "").strip()
    out = []
    for prov in ["gemini", "openai", "grok"]:        # stable display order
        if prov not in enabled:
            continue
        spec = IMAGE_PROVIDER_SPECS[prov]
        out.append({"provider": prov, "label": spec["label"],
                    "default": (prov == default), "params": spec["params"]})
    return out
