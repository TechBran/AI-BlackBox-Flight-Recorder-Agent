"""Shared Google API HttpError -> customer-facing message mapping.

Distinguishes the two 403 causes that are otherwise indistinguishable to a user:
  - SERVICE_DISABLED: the API isn't enabled in the Google Cloud project. The fix
    is enabling it in the Console (APIs & Services > Library) -- re-consenting
    does nothing. Google returns this with details[].reason == "SERVICE_DISABLED".
  - insufficient scope: the operator's stored token predates the scope. The fix
    is to re-run the Google sign-in (reconnect) so the new scope is granted.

Before this split, every 403 mapped to "reconnect", which sent a real debugging
session down the wrong path when the true cause was a disabled API.
"""

import json


def _err(msg):
    return {"error": msg}


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
        return _err(
            f"Google Workspace needs reconnect to grant {app_label} access "
            f"(re-run the Google sign-in in onboarding)"
        )
    reason = getattr(e, "reason", None) or str(e)
    return _err(f"Google {app_label} API error: {reason} (status {status})")
