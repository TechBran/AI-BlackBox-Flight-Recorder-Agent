"""Direct multimodal analysis for the analyze_image and analyze_video tools.

Audit rank 57 (O18-toolvault-modules-01). Both tools used to POST a vision
message to /chat and read ``response`` from the reply. /chat is a task queue
now: it answers {task_id, status} at once, so the tools returned an empty
analysis as a success, while the queued CHAT turn later ran under the operator,
spent a model call and minted a "Describe this image" turn into the operator's
ledger. The tools now call the model here: no task, no chat turn, no ledger
write, and an answer without text is a failure.

The media is a BlackBox upload (/ui/uploads/..., any host's /ui/uploads URL, or
a path inside Portal/uploads, contained by resolve_upload_path) or a public
http(s) URL fetched through the SSRF guard. The model is the box's configured
Gemini default (resolve_default_model), the same one the /chat route used.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
from typing import Tuple
from urllib.parse import urlsplit

import requests

from Orchestrator import config
from Orchestrator.secret_redaction import redact, response_error
from Orchestrator.utils.paths import (
    GEMINI_INLINE_MAX_BYTES,
    IMAGE_EXTS,
    VIDEO_EXTS,
    UploadNotFoundError,
    UploadPathError,
    resolve_upload_path,
)

KINDS = {
    "image": {"exts": IMAGE_EXTS, "mime": "image/"},
    "video": {"exts": VIDEO_EXTS, "mime": "video/"},
}
FETCH_TIMEOUT_SECONDS = 60
# Formats mimetypes does not know on every box.
_MIME_FALLBACKS = {".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
                   ".m4v": "video/x-m4v", ".mkv": "video/x-matroska", ".3gp": "video/3gpp",
                   ".webm": "video/webm", ".mov": "video/quicktime"}


class MediaAnalysisError(Exception):
    """Why an analysis could not be done; the text is for the model and the user."""


def _mime_for(name: str) -> str:
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return _MIME_FALLBACKS.get(suffix) or mimetypes.guess_type("x" + suffix)[0] or ""


def _is_upload_reference(ref: str) -> bool:
    if ref.startswith(("http://", "https://")):
        return urlsplit(ref).path.startswith("/ui/uploads/")
    return True


def load_media(ref: str, kind: str) -> Tuple[bytes, str]:
    """``(bytes, mime_type)`` for the media ``ref`` names (blocking)."""
    spec = KINDS[kind]
    ref = (ref or "").strip()
    if not ref:
        raise MediaAnalysisError(f"{kind}_url is required")
    if _is_upload_reference(ref):
        try:
            path = resolve_upload_path(ref, allowed_exts=spec["exts"], max_bytes=GEMINI_INLINE_MAX_BYTES)
        except UploadNotFoundError as e:
            raise MediaAnalysisError(f"No such upload: {e}") from None
        except UploadPathError as e:
            raise MediaAnalysisError(f"The {kind} must be a BlackBox upload or a public URL: {e}") from None
        mime = _mime_for(path.name)
        if not mime.startswith(spec["mime"]):
            raise MediaAnalysisError(f"{path.name} is not a supported {kind} file")
        return path.read_bytes(), mime

    from Orchestrator.utils.safe_fetch import FetchError, safe_get
    try:
        resp = safe_get(ref, timeout=FETCH_TIMEOUT_SECONDS, max_bytes=GEMINI_INLINE_MAX_BYTES)
    except FetchError as e:
        raise MediaAnalysisError(f"Could not fetch the {kind}: {e}") from None
    if not 200 <= resp.status_code < 300:
        raise MediaAnalysisError(f"Could not fetch the {kind}: HTTP {resp.status_code}")
    header_type = ""
    for name, value in (resp.headers or {}).items():
        if name.lower() == "content-type":
            header_type = str(value).split(";", 1)[0].strip().lower()
    mime = header_type if header_type.startswith(spec["mime"]) else _mime_for(urlsplit(resp.url).path)
    if not mime.startswith(spec["mime"]):
        raise MediaAnalysisError(f"The URL did not return a {kind} (content type {header_type or 'unknown'})")
    return resp.content, mime


def _api_key() -> str:
    return config.GOOGLE_API_KEY or config.GEMINI_API_KEY or ""


def _answer_text(body: dict) -> str:
    """The model's answer, or a MediaAnalysisError saying why there is none."""
    candidates = body.get("candidates") or []
    if not candidates:
        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        raise MediaAnalysisError(
            f"The model returned no analysis{f' (blocked: {blocked})' if blocked else ''}")
    first = candidates[0] or {}
    parts = (first.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    if not text.strip():
        reason = first.get("finishReason")
        raise MediaAnalysisError(
            f"The model returned no analysis{f' (finish reason: {reason})' if reason else ''}")
    return text.strip()


def analyze_blocking(ref: str, prompt: str, kind: str, timeout: float) -> Tuple[str, str]:
    """``(analysis_text, model)`` for one image or video. Raises MediaAnalysisError."""
    key = _api_key()
    if not key:
        raise MediaAnalysisError(
            f"analyze_{kind} needs a Google (Gemini) API key: add one in the setup wizard's API keys step")
    data, mime = load_media(ref, kind)
    model = config.resolve_default_model("google")
    payload = {"contents": [{"role": "user", "parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}},
    ]}]}
    try:
        resp = requests.post(f"{config.GEMINI_BASE_URL}/{model}:generateContent",
                             headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                             json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise MediaAnalysisError(f"Could not reach the model: {type(e).__name__}") from None
    if not 200 <= resp.status_code < 300:
        status, message = response_error(resp)
        raise MediaAnalysisError(f"The model refused the {kind} ({status}): {message}")
    try:
        body = resp.json()
    except ValueError:
        raise MediaAnalysisError("The model answered with something that is not JSON") from None
    return _answer_text(body if isinstance(body, dict) else {}), model


async def analyze_media(ref: str, prompt: str, kind: str, timeout: float) -> Tuple[str, str]:
    """analyze_blocking() off the event loop."""
    try:
        return await asyncio.to_thread(analyze_blocking, ref, prompt, kind, timeout)
    except MediaAnalysisError:
        raise
    except Exception as e:  # noqa: BLE001 — surfaced as the tool's failure text
        raise MediaAnalysisError(redact(f"Analysis failed: {type(e).__name__}: {e}")) from None
