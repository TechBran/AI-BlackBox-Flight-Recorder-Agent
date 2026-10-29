"""Content-Security-Policy for the Portal (/ui), and inert user uploads.

WHY (audit rank 75, P02-voice-web-12): the Portal renders model output, SMS,
email and agent stdout into the page. Everything that reaches innerHTML goes
through DOMPurify, but nothing stopped a sanitizer slip (or a raw sink) from
running script. With this policy the page's script comes only from its own
files: no inline <script>, no inline on*= handler, no eval. The Portal was made
to run under it first (index.html, chat-send.js and the dev FX Lab held the
last inline handlers and scripts).

THE POLICY (portal_csp):
  * script-src <host>/ui/ — the Portal's modules, its vendored libraries
    (/ui/vendor/, no CDN) and the mic-capture worklet, and nothing else on
    the box: 'self' would also trust /app-proxy/{port}/*.js (agent-written
    apps, served with their own script types) and every other same-origin
    route. Not 'unsafe-inline', not 'unsafe-eval'. worker-src the same. A
    host that cannot be a CSP host-source (an IPv6 literal, a strange Host)
    falls back to 'self' rather than leave the Portal with no script at all.
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

THE PAGES: every /ui response, and "/" — the documented entry point (README,
`tailscale serve`, the wizard's hand-off), which serves the same
Portal/index.html.

UPLOADS (/ui/uploads/): the /ui/ script source would also trust whatever an
operator or a tool put in Portal/uploads/ (the upload route keeps any
extension). So upload responses get `X-Content-Type-Options: nosniff`; a
script type is re-labelled text/plain, which a <script src> then refuses, and
a document type (HTML, SVG, any XML) gets `Content-Security-Policy: sandbox`,
which renders it in an opaque origin with no script. Media is untouched.
An upload is the FILE the mount opens, never the URL's spelling: StaticFiles
normalises the path first, so /ui//uploads/x, /ui/%2Fuploads/x (decoded
before routing) and /ui/./uploads/x all serve Portal/uploads/x. The
middleware classifies the path the same way (is_upload_path), and the /ui
mount (GuardedStaticFiles, media_headers=harden_upload_headers) hardens by
the resolved file as well, so a name that symlinks into uploads/ is covered.

A pure ASGI middleware: only "/" and /ui responses are touched, everything
else (API, SSE streams, WebSockets) passes straight through.
"""
from __future__ import annotations

import posixpath
import re
from typing import Optional

from starlette.datastructures import MutableHeaders

from Orchestrator.utils.static_guard import PORTAL_MEDIA_DIRS

PORTAL_PREFIX = "/ui"
# The one non-/ui path that serves the Portal document (admin_routes.root).
PORTAL_ROOT_DOCUMENT = "/"
_MEDIA_DIRS = frozenset(d.lower() for d in PORTAL_MEDIA_DIRS)

# A Host header we may echo into the policy: host or [v6], optional :port.
_SAFE_HOST = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
# ...and one a CSP host-source can name (no IPv6 literal).
_SOURCE_HOST = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")

# Script types a browser would execute from <script src> (or a worker).
_SCRIPT_TYPES = frozenset({
    "text/javascript", "application/javascript", "application/x-javascript",
    "text/ecmascript", "application/ecmascript", "text/jscript",
    "application/wasm",
})
# Types a browser renders as an active document when it navigates to them,
# plus every XML type (_is_document_type): an XML document runs an
# XHTML-namespaced <script>, and a browser renders +xml types (atom, rss,
# xslt, rdf, kml, ...) as XML.
_DOCUMENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
    "application/xml", "text/xsl", "text/x-component", "application/hta",
})


def portal_csp(host: Optional[str]) -> str:
    """The Portal policy; script-src/worker-src name the request's own host's
    /ui/ tree and connect-src its ws(s) endpoints. No scheme on the script
    source: it then matches the page's own (http on the LAN, https through
    `tailscale serve`, which preserves Host)."""
    scripts = "'self'"
    if host and _SOURCE_HOST.match(host):
        scripts = f"{host}{PORTAL_PREFIX}/"
    connect = "connect-src 'self' data: blob:"
    if host and _SAFE_HOST.match(host):
        connect += f" ws://{host} wss://{host}"
    return "; ".join((
        "default-src 'self'",
        f"script-src {scripts}",
        "style-src 'self' 'unsafe-inline'",
        "img-src * data: blob:",
        "media-src * data: blob:",
        connect,
        "font-src 'self' data:",
        "frame-src 'self'",
        f"worker-src {scripts}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ))


def _is_portal_path(path: str) -> bool:
    return path == PORTAL_PREFIX or path.startswith(PORTAL_PREFIX + "/")


def is_upload_path(path: str) -> bool:
    """Whether a request path names a file in a Portal media dir (uploads/),
    resolved the way StaticFiles resolves it before opening the file: '//',
    '.' and '..' segments collapse (normpath of the joined segments), so no
    spelling of the path escapes the classification. Case-insensitive, for a
    filesystem that is."""
    if not _is_portal_path(path):
        return False
    route = path[len(PORTAL_PREFIX):]
    rel = posixpath.normpath(posixpath.join(*route.split("/")))
    return rel.split("/")[0].lower() in _MEDIA_DIRS


def _media_type(headers: MutableHeaders) -> str:
    return (headers.get("content-type") or "").split(";")[0].strip().lower()


def _is_document_type(kind: str) -> bool:
    return kind in _DOCUMENT_TYPES or kind.endswith("+xml")


def harden_upload_headers(headers: MutableHeaders) -> None:
    """Make an upload response inert as script and as a document."""
    headers["X-Content-Type-Options"] = "nosniff"
    kind = _media_type(headers)
    if kind in _SCRIPT_TYPES:
        headers["Content-Type"] = "text/plain; charset=utf-8"
    elif _is_document_type(kind):
        headers["Content-Security-Policy"] = "sandbox"


class PortalCSPMiddleware:
    """Adds the Portal's CSP to "/" and /ui responses and makes /ui/uploads/
    inert."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == PORTAL_ROOT_DOCUMENT or _is_portal_path(path)):
            await self.app(scope, receive, send)
            return
        upload = is_upload_path(path)
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
                elif "content-security-policy" not in headers:
                    # A response the /ui mount already made inert (its file
                    # resolved into uploads/) keeps its sandbox.
                    headers["Content-Security-Policy"] = portal_csp(host)
            await send(message)

        await self.app(scope, receive, send_with_policy)
