"""Content-Security-Policy for the Portal (/ui), and inert user uploads.

WHY (audit rank 75, P02-voice-web-12): the Portal renders model output, SMS,
email and agent stdout into the page. Everything that reaches innerHTML goes
through DOMPurify, but nothing stopped a sanitizer slip (or a raw sink) from
running script. With this policy the page's script comes only from its own
files: no inline <script>, no inline on*= handler, no eval. The Portal was made
to run under it first (index.html, chat-send.js and the dev FX Lab held the
last inline handlers and scripts).

THE POLICY (PORTAL_CSP):
  * script-src 'self' — the Portal's modules and its vendored libraries
    (/ui/vendor/, no CDN). Not 'unsafe-inline', not 'unsafe-eval'.
  * style-src 'self' 'unsafe-inline' — the markup and the modules set inline
    styles and inject <style> blocks everywhere; styles cannot run script.
  * img-src / media-src * data: blob: — replies embed images from anywhere,
    TTS plays data:/blob: audio, voice previews stream from the provider.
  * connect-src 'self' plus ws(s):// for the request's own host — every API
    call and WebSocket is same-origin (Tailscale serve preserves Host); data:
    and blob: because a download fetches the TTS data: URL into a Blob. A Host
    that is not a plain host[:port] gets no ws(s) source beyond 'self'.
  * frame-src 'self' — /app-proxy/{port}/ and /cu/view/ are same-origin.
  * object-src 'none', base-uri 'self', form-action 'self'.

UPLOADS (/ui/uploads/): 'self' would also trust whatever an operator or a tool
put in Portal/uploads/ (the upload route keeps any extension). So upload
responses get `X-Content-Type-Options: nosniff`; a script type is re-labelled
text/plain, which a <script src> then refuses, and a document type (HTML, SVG,
XML) gets `Content-Security-Policy: sandbox`, which renders it in an opaque
origin with no script. Media is untouched.

A pure ASGI middleware: only /ui responses are touched, everything else
(API, SSE streams, WebSockets) passes straight through.
"""
from __future__ import annotations

import re
from typing import Optional

from starlette.datastructures import MutableHeaders

PORTAL_PREFIX = "/ui"
UPLOADS_PREFIX = "/ui/uploads/"

# A Host header we may echo into the policy: host or [v6], optional :port.
_SAFE_HOST = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")

_BASE_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src * data: blob:",
    "media-src * data: blob:",
    "font-src 'self' data:",
    "frame-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
)

# Script types a browser would execute from <script src> (or a worker).
_SCRIPT_TYPES = frozenset({
    "text/javascript", "application/javascript", "application/x-javascript",
    "text/ecmascript", "application/ecmascript", "text/jscript",
    "application/wasm",
})
# Types a browser renders as an active document when it navigates to them.
_DOCUMENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
    "application/xml", "text/xsl", "text/x-component", "application/hta",
})


def portal_csp(host: Optional[str]) -> str:
    """The /ui policy; connect-src names the request's own host for ws(s)."""
    connect = "connect-src 'self' data: blob:"
    if host and _SAFE_HOST.match(host):
        connect += f" ws://{host} wss://{host}"
    return "; ".join(_BASE_DIRECTIVES[:5] + (connect,) + _BASE_DIRECTIVES[5:])


def _is_portal_path(path: str) -> bool:
    return path == PORTAL_PREFIX or path.startswith(PORTAL_PREFIX + "/")


def _media_type(headers: MutableHeaders) -> str:
    return (headers.get("content-type") or "").split(";")[0].strip().lower()


def harden_upload_headers(headers: MutableHeaders) -> None:
    """Make an upload response inert as script and as a document."""
    headers["X-Content-Type-Options"] = "nosniff"
    kind = _media_type(headers)
    if kind in _SCRIPT_TYPES:
        headers["Content-Type"] = "text/plain; charset=utf-8"
    elif kind in _DOCUMENT_TYPES:
        headers["Content-Security-Policy"] = "sandbox"


class PortalCSPMiddleware:
    """Adds the Portal's CSP to /ui responses and makes /ui/uploads/ inert."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_portal_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        upload = scope["path"].startswith(UPLOADS_PREFIX)
        host = None
        for name, value in scope.get("headers") or ():
            if name == b"host":
                host = value.decode("latin-1")
                break

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if upload:
                    harden_upload_headers(headers)
                else:
                    headers["Content-Security-Policy"] = portal_csp(host)
            await send(message)

        await self.app(scope, receive, send_with_policy)
