"""What BlackBox accepts as a Google Cloud service-account key file.

GOOGLE_APPLICATION_CREDENTIALS is handed to google-auth, which honours far
more than service accounts there. An `external_account` config names a local
file (it could be .env) as its subject token and POSTs that file's contents to
a token_url chosen by whoever wrote the config, on the next refresh; a
service-account file's token_uri receives a POST too. So a file is accepted
only when it is a service-account key whose token endpoint is Google's.

Used by the credentials upload (before anything is written) and by the one
google.auth.default() caller (rerank's Vertex path), which re-checks the file
in case an older build accepted something else.
"""
from __future__ import annotations

import json
from typing import Any, Optional

# The token endpoints Google has written into service-account key files.
GOOGLE_TOKEN_URIS = frozenset({
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",      # older key files
    "https://www.googleapis.com/oauth2/v4/token",
})

MAX_KEY_FILE_BYTES = 64 * 1024     # service-account keys are ~2-3 KB


def service_account_problem(data: Any) -> Optional[str]:
    """None when `data` (a parsed JSON value) is a Google service-account
    key, otherwise why not, phrased for the wizard."""
    if not isinstance(data, dict):
        return "the JSON must be an object (a service-account key file)"
    if data.get("type") != "service_account":
        return ("only a Google Cloud service-account key is accepted "
                f"(this file's type is {str(data.get('type'))[:40]!r})")
    for field in ("client_email", "private_key"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"the service-account key has no {field}"
    if data.get("token_uri") not in GOOGLE_TOKEN_URIS:
        return "the service-account key's token_uri is not Google's token endpoint"
    return None


def service_account_file_problem(path: str) -> Optional[str]:
    """service_account_problem() for a file on disk (unreadable = a problem)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_KEY_FILE_BYTES + 1)
    except OSError as e:
        return f"cannot read {path}: {e.strerror or e}"
    if len(raw) > MAX_KEY_FILE_BYTES:
        return "the file is too large to be a service-account key"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "the file is not JSON"
    return service_account_problem(data)
