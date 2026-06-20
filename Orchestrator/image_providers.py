"""Per-provider image generation adapters.

Each adapter has the uniform signature ``(prompt: str, options: dict) -> list[bytes]``
and returns raw image bytes (PNG/JPEG) ready for the task worker's save-loop.

Provider selection happens in ``Orchestrator/tasks.py`` (``process_image_generation``)
via ``IMAGE_PROVIDERS`` keyed by the ``provider`` field on the task options.
Untagged tasks fall back to ``DEFAULT_IMAGE_PROVIDER`` (gemini) for back-compat
with legacy ``/generate/image`` callers.
"""
import base64
import requests

from Orchestrator.config import OPENAI_API_KEY, XAI_API_KEY, GOOGLE_IMAGEN_MODEL

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
XAI_IMAGES_URL = "https://api.x.ai/v1/images/generations"
OPENAI_IMAGE_MODEL = "gpt-image-1"          # quality-first gpt-image tier (spike-verified)
XAI_IMAGE_MODEL = "grok-imagine-image-quality"


def _openai_images(prompt, options):
    n = int(options.get("numberOfImages") or options.get("n") or 1)
    body = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "n": n}
    if options.get("size"):
        body["size"] = options["size"]
    if options.get("quality"):
        body["quality"] = options["quality"]
    r = requests.post(
        OPENAI_IMAGES_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=body, timeout=180)
    r.raise_for_status()
    return [base64.b64decode(d["b64_json"]) for d in r.json().get("data", []) if d.get("b64_json")]


def _xai_images(prompt, options):
    n = int(options.get("numberOfImages") or options.get("n") or 1)
    body = {"model": XAI_IMAGE_MODEL, "prompt": prompt, "n": n}
    # xAI's images endpoint is OpenAI-compatible; per xAI docs the aspect-ratio
    # field is `aspect_ratio`. Send it when set so grok's advertised aspectRatio
    # actually applies; harmless (ignored) if a given model build doesn't honor it.
    if options.get("aspectRatio"):
        body["aspect_ratio"] = options["aspectRatio"]
    r = requests.post(
        XAI_IMAGES_URL,
        headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
        json=body, timeout=180)
    r.raise_for_status()
    out = []
    for d in r.json().get("data", []):
        if d.get("b64_json"):
            out.append(base64.b64decode(d["b64_json"]))
        elif d.get("url"):
            img = requests.get(d["url"], timeout=120)  # temp imgen.x.ai URL -- fetch immediately
            img.raise_for_status()
            out.append(img.content)
    return out


def _gemini_images(prompt, options):
    from Orchestrator.routes.tts_routes import call_imagen  # lazy: avoid import cycle
    return call_imagen(prompt, GOOGLE_IMAGEN_MODEL, options)


IMAGE_PROVIDERS = {"gemini": _gemini_images, "openai": _openai_images, "grok": _xai_images}
DEFAULT_IMAGE_PROVIDER = "gemini"

# tool name -> provider (dispatch sites map the called image tool to its provider)
IMAGE_TOOL_PROVIDERS = {"gemini_image": "gemini", "openai_image": "openai", "grok_image": "grok"}
