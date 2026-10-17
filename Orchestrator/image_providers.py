"""Per-provider image generation adapters.

Each adapter has the uniform signature ``(prompt: str, options: dict) -> list[bytes]``
and returns raw image bytes (PNG/JPEG) ready for the task worker's save-loop.

Provider selection happens in ``Orchestrator/tasks.py`` (``process_image_generation``)
via ``IMAGE_PROVIDERS`` keyed by the ``provider`` field on the task options.
Untagged tasks fall back to ``DEFAULT_IMAGE_PROVIDER`` (gemini) for back-compat
with legacy ``/generate/image`` callers.
"""
import base64
from urllib.parse import urljoin, urlsplit

import requests

from Orchestrator.config import OPENAI_API_KEY, XAI_API_KEY, GOOGLE_IMAGEN_MODEL
from Orchestrator.utils.safe_fetch import FetchError, safe_get

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
XAI_IMAGES_URL = "https://api.x.ai/v1/images/generations"
OPENAI_IMAGE_MODEL = "gpt-image-1"          # quality-first gpt-image tier (spike-verified)
XAI_IMAGE_MODEL = "grok-imagine-image-quality"

# A provider may answer with a URL instead of inline bytes, and the box then
# fetches it on its own behalf -- so the response decides where the box
# connects. A hostile or broken one must not point it at the box's own
# loopback services, the LAN or cloud metadata (audit M3 U20).
IMAGE_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 120
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _download_public_image(url):
    """A cloud provider's temporary image URL: public internet only (the SSRF
    guard vets every address and redirect hop and caps the body)."""
    try:
        r = safe_get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT, max_bytes=IMAGE_DOWNLOAD_MAX_BYTES)
    except FetchError as e:
        raise RuntimeError(f"Image download refused or failed: {e}") from None
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"Image download failed: HTTP {r.status_code}")
    return r.content


def _origin(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)   # ValueError on a bad port
    return scheme, (parts.hostname or "").lower(), port


def _download_from_server(url, base_url):
    """A LAN image server's own result URL. The server is registered, its
    reply is not: the URL must stay on the registered origin (relative URLs
    resolve against it), no redirect is followed and the body is capped."""
    try:
        target = urljoin(base_url.rstrip("/") + "/", url)
        parts = urlsplit(target)
        allowed = (parts.username is None and parts.password is None
                   and parts.scheme.lower() in _DEFAULT_PORTS
                   and _origin(target) == _origin(base_url))
    except (TypeError, ValueError):     # not a string; a bad port
        allowed = False
    if not allowed:
        raise RuntimeError(
            f"Local image server returned a URL outside its own origin ({base_url}); refusing to fetch it")
    r = requests.get(target, timeout=IMAGE_DOWNLOAD_TIMEOUT, allow_redirects=False, stream=True)
    try:
        if 300 <= r.status_code < 400:
            raise RuntimeError("Local image server answered the image URL with a redirect; refusing to follow it")
        if not 200 <= r.status_code < 300:
            raise RuntimeError(f"Image download failed: HTTP {r.status_code}")
        body = bytearray()
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > IMAGE_DOWNLOAD_MAX_BYTES:
                raise RuntimeError(
                    f"Image download is larger than {IMAGE_DOWNLOAD_MAX_BYTES} bytes; refusing it")
        return bytes(body)
    finally:
        r.close()


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
            out.append(_download_public_image(d["url"]))  # temp imgen.x.ai URL -- fetch immediately
    return out


def _gemini_images(prompt, options):
    from Orchestrator.routes.tts_routes import call_imagen  # lazy: avoid import cycle
    return call_imagen(prompt, GOOGLE_IMAGEN_MODEL, options)


def _local_images(prompt, options):
    """FREE local text-to-image via a registered OpenAI-compatible LAN server
    (Z-Image Turbo / stable-diffusion.cpp). Credentials come from the custom-
    server registry (custom_models.json) -- never hardcoded. 180s timeout absorbs
    a cold llama-swap swap (~35s); the server queues one request at a time."""
    from Orchestrator.onboarding.custom_servers import resolve_image_server  # lazy: no import-time cost / cycle
    resolved = resolve_image_server(options.get("model"))
    if not resolved:
        raise RuntimeError(
            "No local image model available -- add an OpenAI-compatible server "
            "hosting an image model (e.g. z-image) in the onboarding wizard.")
    srv, model = resolved
    options["_resolved_image_model"] = model  # surface to the worker for accurate provenance
    n = int(options.get("numberOfImages") or options.get("n") or 1)
    body = {"model": model, "prompt": prompt, "n": n, "output_format": "png"}
    if options.get("size"):
        body["size"] = options["size"]
    headers = {"Content-Type": "application/json"}
    api_key = srv.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(f"{srv['base_url']}/images/generations",
                      headers=headers, json=body, timeout=180)
    r.raise_for_status()
    out = []
    for d in r.json().get("data", []):
        if d.get("b64_json"):
            out.append(base64.b64decode(d["b64_json"]))
        elif d.get("url"):
            out.append(_download_from_server(d["url"], srv["base_url"]))  # some sd.cpp builds return a URL
    if not out:
        # Fail LOUDLY -- a zero-image response must not save nothing and "succeed".
        raise RuntimeError("Local image server returned no decodable image data")
    return out


IMAGE_PROVIDERS = {"gemini": _gemini_images, "openai": _openai_images,
                   "grok": _xai_images, "local": _local_images}
DEFAULT_IMAGE_PROVIDER = "gemini"

# tool name -> provider (dispatch sites map the called image tool to its provider)
IMAGE_TOOL_PROVIDERS = {"gemini_image": "gemini", "openai_image": "openai",
                        "grok_image": "grok", "local_image": "local"}
