"""X-Twilio-Signature verification for the Twilio voice webhooks.

Twilio signs every webhook it sends (https://www.twilio.com/docs/usage/webhooks/webhooks-security):

    data      = full URL Twilio requested + every POST param as name+value,
                names sorted, each name's unique values sorted
    signature = base64( HMAC-SHA1(auth_token, data) )

This is a stdlib restatement of twilio-python's RequestValidator (the SDK is
not a dependency of this box). Like RequestValidator, the URL is tried with
and without its default port. Only form-encoded webhooks are handled; the
JSON/bodySHA256 variant is not used by any route here.

Behind `tailscale serve` / Funnel (and ngrok-style tunnels) uvicorn sees a
loopback request, but Twilio signed the PUBLIC URL. The candidates are, in
order: X-Forwarded-Proto/-Host (or the preserved Host), the same host on
https, and TWILIO_WEBHOOK_BASE_URL + path. Trying several candidates does not
weaken the check: every one still needs the account's auth token.

require_twilio_signature is the FastAPI dependency every Twilio-originated
POST route carries. It fails closed: no TWILIO_AUTH_TOKEN, no header, or no
matching candidate -> 403. The reason is logged server-side (path only,
never params, which carry Digits).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, Request

from Orchestrator import config


def _pairs(params) -> list[tuple[str, str]]:
    if params is None:
        return []
    if isinstance(params, Mapping):
        return [(str(k), str(v)) for k, v in params.items()]
    return [(str(k), str(v)) for k, v in params]


def compute_signature(auth_token: str, url: str, params: Mapping | Iterable[tuple[str, str]] | None) -> str:
    """Twilio's signature for ``url`` + ``params`` (a mapping or (name, value) pairs)."""
    grouped: dict[str, set[str]] = {}
    for name, value in _pairs(params):
        grouped.setdefault(name, set()).add(value)
    data = url + "".join(name + value for name in sorted(grouped) for value in sorted(grouped[name]))
    mac = hmac.new((auth_token or "").encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _port_variants(url: str) -> list[str]:
    """``url`` without and with its port (Twilio signs either form).

    Splits the netloc by hand (no .hostname/.port): those lowercase the host
    and raise on a junk port in an attacker-supplied Host header.
    """
    parts = urlsplit(url)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    prefix = f"{userinfo}@" if userinfo else ""
    if hostport.startswith("["):  # IPv6 literal
        host, _, rest = hostport.partition("]")
        host += "]"
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = hostport.partition(":")
    default = "443" if parts.scheme == "https" else "80"
    without = urlunsplit(parts._replace(netloc=prefix + host))
    with_port = urlunsplit(parts._replace(netloc=f"{prefix}{host}:{port or default}"))
    return [without, with_port]


def validate(auth_token: str, url: str, params, signature) -> bool:
    """True when ``signature`` is Twilio's signature of ``url`` + ``params``."""
    if not auth_token or not isinstance(signature, str) or not signature:
        return False
    supplied = signature.encode("utf-8")
    valid = False
    for candidate in _port_variants(url):
        expected = compute_signature(auth_token, candidate, params).encode("ascii")
        # bytes compare: compare_digest on str raises for non-ASCII input.
        valid |= hmac.compare_digest(expected, supplied)
    return valid


def _first(header_value: str) -> str:
    return (header_value or "").split(",")[0].strip()


def candidate_urls(*, scheme: str, host_header: str, forwarded_proto: str,
                   forwarded_host: str, path: str, query: str,
                   configured_base: str) -> list[str]:
    """Public URLs Twilio may have signed for this request, most likely first."""
    tail = path + (f"?{query}" if query else "")
    host = _first(forwarded_host) or (host_header or "").strip()
    proto = _first(forwarded_proto).lower() or scheme
    urls = []
    if host:
        urls.append(f"{proto}://{host}{tail}")
        urls.append(f"https://{host}{tail}")
    base = (configured_base or "").strip().rstrip("/")
    if base:
        base = base.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        urls.append(base + tail)
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def _request_candidates(request: Request) -> list[str]:
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else request.url.path
    query = (request.scope.get("query_string") or b"").decode("latin-1")
    return candidate_urls(
        scheme=request.url.scheme,
        host_header=request.headers.get("host", ""),
        forwarded_proto=request.headers.get("x-forwarded-proto", ""),
        forwarded_host=request.headers.get("x-forwarded-host", ""),
        path=path,
        query=query,
        configured_base=str(getattr(config, "TWILIO_WEBHOOK_BASE_URL", "") or ""),
    )


async def require_twilio_signature(request: Request) -> None:
    """FastAPI dependency: 403 unless the request carries a valid Twilio signature."""
    token = str(getattr(config, "TWILIO_AUTH_TOKEN", "") or "")
    path = request.url.path
    if not token:
        print(f"[TWILIO] REJECTED {path}: TWILIO_AUTH_TOKEN is not configured, "
              f"so webhook signatures cannot be verified")
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    signature = request.headers.get("x-twilio-signature", "")
    if not signature:
        print(f"[TWILIO] REJECTED {path}: missing X-Twilio-Signature")
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    form = await request.form()
    params = list(form.multi_items())
    for url in _request_candidates(request):
        if validate(token, url, params, signature):
            return
    print(f"[TWILIO] REJECTED {path}: X-Twilio-Signature does not match "
          f"(check TWILIO_AUTH_TOKEN and that the public URL reaches this box unchanged)")
    raise HTTPException(status_code=403, detail="invalid Twilio signature")
