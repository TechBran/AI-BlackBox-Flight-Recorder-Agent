"""Shared Google API HttpError -> customer-facing message mapping.

Distinguishes the two 403 causes that are otherwise indistinguishable to a user:
  - SERVICE_DISABLED: the API isn't enabled in the Google Cloud project. The fix
    is enabling it in the Console (APIs & Services > Library) -- re-consenting
    does nothing. Google returns this with details[].reason == "SERVICE_DISABLED".
  - insufficient scope: the operator's stored token predates the scope. The fix
    is to re-run the Google sign-in (reconnect) so the new scope is granted.

Before this split, every 403 mapped to "reconnect", which sent a real debugging
session down the wrong path when the true cause was a disabled API. Google also
answers 403 (not 429) for usageLimits — rateLimitExceeded,
userRateLimitExceeded, dailyLimitExceeded — a quota blip that reconnecting
does not fix either.
"""

import json


# Reasons Google gives a 403 for a token that lacks a scope (errors[].reason in
# the v1 body, details[].reason in the google.rpc.ErrorInfo one).
_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
# Reasons for a 403 that is a quota/rate limit: transient, nothing to reconnect.
_RATE_LIMIT_REASONS = frozenset({
    "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
    "quotaExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED",
})


def _err(msg):
    return {"error": msg}


def _error_body(e):
    """The parsed ``error`` object of an HttpError's JSON body, or {}."""
    try:
        content = getattr(e, "content", None)
        if hasattr(content, "decode"):
            content = content.decode("utf-8", "replace")
        body = json.loads(content)
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {}
    except Exception:
        return {}


def _error_reasons(e):
    """Every reason/domain an HttpError body names (errors[] and details[])."""
    error = _error_body(e)
    found = set()
    for key in ("errors", "details"):
        items = error.get(key)
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict):
                for field in ("reason", "domain"):
                    if item.get(field):
                        found.add(str(item[field]))
    return found


def _is_scope_insufficient(e):
    """True if a 403 HttpError says the token lacks a scope (fixed by re-consenting)."""
    if _error_reasons(e) & _SCOPE_REASONS:
        return True
    return "insufficient authentication scopes" in str(_error_body(e).get("message", "")).lower()


def _is_rate_limited(e):
    """True if a 403 HttpError is a usage/rate limit (transient; retry later)."""
    reasons = _error_reasons(e)
    return bool(reasons & _RATE_LIMIT_REASONS) or "usageLimits" in reasons


def _is_service_disabled(e):
    """True if a 403 HttpError is Google's SERVICE_DISABLED (API not enabled).

    Parses the JSON error body for details[].reason == "SERVICE_DISABLED".
    Returns False on any parse failure (degrades to the reconnect message).
    """
    try:
        content = getattr(e, "content", None)
        if hasattr(content, "decode"):
            content = content.decode("utf-8", "replace")
        body = json.loads(content)
        for d in body.get("error", {}).get("details", []):
            if d.get("reason") == "SERVICE_DISABLED":
                return True
    except Exception:
        pass
    return False


def http_error_to_dict(e, app_label):
    """Map a googleapiclient HttpError to a customer-facing error dict.

    `app_label` is the human API name, e.g. "Docs"/"Sheets"/"Slides"/"Drive"/
    "Calendar". A 403 is split into the disabled-API vs reconnect cases; any
    other status returns a generic message carrying the reason + status.
    """
    status = getattr(e, "status_code", None)
    if status is None:
        resp = getattr(e, "resp", None)
        status = getattr(resp, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 403:
        if _is_service_disabled(e):
            return _err(
                f"Google {app_label} API is not enabled in your Google Cloud "
                f"project. Enable it in the Cloud Console (APIs & Services > "
                f"Library), wait a minute, then retry."
            )
        if _is_rate_limited(e) and not _is_scope_insufficient(e):
            return _err(
                f"Google {app_label} API rate limit reached (status 403); "
                f"wait a minute, then retry."
            )
        return _err(
            f"Google Workspace needs reconnect to grant {app_label} access "
            f"(re-run the Google sign-in in onboarding)"
        )
    reason = getattr(e, "reason", None) or str(e)
    return _err(f"Google {app_label} API error: {reason} (status {status})")
