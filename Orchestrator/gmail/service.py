"""Gmail API service — per-operator OAuth token management + API calls."""

import base64
import json
import os
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Dict, List, Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from Orchestrator.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from Orchestrator.utils import atomic_json
from Orchestrator.utils.paths import resolve

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/calendar",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
# The token store. None = Manifest/gmail_tokens under the project root
# (BLACKBOX_ROOT first), resolved on use rather than relative to the process
# CWD; created lazily (owner-only) by save_tokens, not at import. Tests point it
# at a temp dir.
TOKEN_DIR: Optional[Path] = None


class GoogleReauthRequired(RefreshError):
    """The operator's stored Google grant can no longer be refreshed.

    Google answered the refresh with an OAuth error (``invalid_grant`` when the
    grant was revoked or expired, ``invalid_client`` when the OAuth client
    changed, ...): only a fresh sign-in fixes it. A RefreshError subclass, so
    existing ``except RefreshError`` handlers still catch it; ``str()`` is the
    customer-facing message tools and cron history carry.
    """

    def __init__(self, operator: str, code: str, detail: str = ""):
        self.operator = operator
        self.code = code
        self.detail = detail
        super().__init__(reauth_message(operator, code))


def reauth_message(operator: str, code: str) -> str:
    """The one wording for "this operator must sign in to Google again"."""
    return (
        f"Google sign-in for {operator} is no longer valid ({code}). Reconnect Google "
        f"Workspace for this operator: Portal Tools > Google Workspace > Reconnect, "
        f"or Android Settings > Google Workspace > Reconnect."
    )


def oauth_error_code(exc: BaseException) -> str:
    """Google's OAuth error code from a RefreshError ('invalid_grant', ...).

    google-auth raises RefreshError('invalid_grant: Bad Request',
    {'error': 'invalid_grant', ...}); prefer the structured body, fall back to
    the 'code:' prefix of the message.
    """
    args = getattr(exc, "args", ()) or ()
    if len(args) > 1 and isinstance(args[1], dict) and args[1].get("error"):
        return str(args[1]["error"])
    m = re.match(r"\s*([a-z_]+):", str(args[0]) if args else "")
    return m.group(1) if m else "refresh_failed"


def reauth_error_code(exc: BaseException) -> Optional[str]:
    """The error code when ``exc`` proves the stored grant is unusable, else None.

    Dead grant: a non-retryable RefreshError (raised by our refresh or by the
    API transport's own 401-refresh), an HTTP 401, or an HTTP 403 whose body
    says the token lacks a scope (fixed by re-consenting). Anything else —
    network trouble, a 5xx, a 403 rate/quota limit (Gmail answers those with
    403, not 429), a Google API that is disabled in the Cloud project, a 403
    whose reason is unknown — says nothing about the grant, so it returns None
    and nobody is told to sign in again.
    """
    if isinstance(exc, GoogleReauthRequired):
        return exc.code
    if isinstance(exc, RefreshError):
        return None if getattr(exc, "retryable", False) else oauth_error_code(exc)
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return None
        if status == 401:
            return "unauthorized"
        if status == 403:
            from Orchestrator.google_workspace._errors import _is_scope_insufficient
            return "insufficient_scope" if _is_scope_insufficient(exc) else None
    return None


def is_error_result(result: Any) -> bool:
    """True when a Gmail helper's return value is its error shape.

    The helpers never raise for an API problem: they return {"error": ...}, or
    [{"error": ...}] from the list-returning ones. An empty list (no matches)
    is a successful result.
    """
    if isinstance(result, dict):
        return "error" in result
    if isinstance(result, list):
        return any(isinstance(item, dict) and "error" in item for item in result)
    return False


def _api_error(prefix: str, operator: str, exc: BaseException) -> str:
    """Error text for a failed Gmail call: reconnect guidance for a dead grant."""
    code = reauth_error_code(exc)
    return reauth_message(operator, code) if code else f"{prefix}: {exc}"


def _parse_expiry(value: Any) -> Optional[datetime]:
    """Stored ISO-8601 expiry -> the naive-UTC datetime google-auth compares against."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _format_expiry(dt: Optional[datetime]) -> Optional[str]:
    """google-auth's naive-UTC expiry -> ISO-8601 'Z' (Credentials.to_json's format)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def token_data_from_credentials(creds) -> dict:
    """The token-file record for a freshly granted credential.

    Carries the access token's expiry so _get_credentials knows when to
    refresh. Deliberately NO client_id/client_secret: both come from config at
    load time, so the token file never holds a copy of the OAuth client secret.
    """
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri or TOKEN_URI,
        "expiry": _format_expiry(getattr(creds, "expiry", None)),
        "scopes": list(creds.scopes) if creds.scopes else list(SCOPES),
    }


def _token_path(operator: str) -> Path:
    """Get token file path for an operator."""
    safe_name = operator.replace("/", "_").replace("\\", "_")
    token_dir = TOKEN_DIR if TOKEN_DIR is not None else resolve("Manifest", "gmail_tokens")
    return token_dir / f"{safe_name}.json"


def save_tokens(operator: str, token_data: dict):
    """Save an operator's OAuth tokens: owner-only (0600 file, 0700 dir), atomic.

    A refresh token grants the operator's whole Google account (mail, Drive,
    Calendar), so the file is written to a private temp sibling and swapped in
    with os.replace — a reader sees the old or the new file, never half of one.
    """
    path = _token_path(operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    atomic_json.write_json(path, token_data, mode=0o600)


def load_tokens(operator: str) -> Optional[dict]:
    """Load OAuth tokens from disk for an operator. A file that does not parse
    raises (ValueError): probe_connection reports it as token_unreadable, and
    the next save_tokens moves it aside before writing (atomic_json)."""
    path = _token_path(operator)
    if path.exists():
        return json.loads(path.read_text())
    return None


def delete_tokens(operator: str):
    """Remove stored tokens (logout)."""
    path = _token_path(operator)
    if path.exists():
        path.unlink()


def is_connected(operator: str) -> bool:
    """True iff the operator has a stored token with a refresh_token.

    Presence only — it does not prove Google still accepts the grant; that is
    probe_connection's job (GET /gmail/status).
    """
    tokens = load_tokens(operator)
    return tokens is not None and "refresh_token" in tokens


def workspace_connected(operator: str) -> bool:
    """True iff a stored token with a refresh_token exists for the operator.
    The Workspace tools use this to return a clear "connect Google" message.
    """
    return is_connected(operator)


class _PersistingCredentials(Credentials):
    """Credentials that write every refresh back to the operator's token file.

    Both refreshes go through here: the one _get_credentials runs when the token
    is due, and the one the API client's transport runs when Google answers 401
    (an access token revoked or expired early). The second used to live only in
    that one call's object, so every later tool call paid a failed request plus
    a refresh round trip again (audit O12-messaging-15).
    """

    _bbx_operator: str = ""
    _bbx_tokens: Optional[dict] = None

    def refresh(self, request):
        super().refresh(request)
        if self._bbx_operator:
            self._bbx_tokens = _save_refreshed(self._bbx_operator, self._bbx_tokens or {}, self)


def _get_credentials(operator: str) -> Optional[Credentials]:
    """Build (and refresh if due) shared OAuth credentials for an operator.

    Returns None when the operator has no token or no refresh_token. Raises
    GoogleReauthRequired when Google refuses the refresh (revoked/expired
    grant), and lets transient refresh/network errors propagate — a dead or
    unreachable grant is never disguised as "not connected".
    These credentials are scoped for Gmail + Docs/Sheets/Slides/Drive/Calendar.
    """
    tokens = load_tokens(operator)
    if not tokens or "refresh_token" not in tokens:
        return None

    creds = _PersistingCredentials(
        token=tokens.get("access_token"),
        refresh_token=tokens["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
        expiry=_parse_expiry(tokens.get("expiry")),
    )
    creds._bbx_operator = operator
    creds._bbx_tokens = tokens

    # Refresh when the access token is expired or missing — and when its age is
    # unknown: a token file written before expiry was persisted is refreshed
    # once, after which its expiry is on disk. (Without an expiry google-auth
    # treats the token as never expiring, so it was never refreshed here.)
    if creds.expiry is None or not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            if getattr(e, "retryable", False):
                raise
            print(f"[Google] Token refresh refused for {operator}: {e}")
            raise GoogleReauthRequired(operator, oauth_error_code(e), str(e)) from e

    return creds


def _save_refreshed(operator: str, tokens: dict, creds: Credentials) -> dict:
    """Persist a refreshed access token + its expiry (and a rotated refresh token).

    Also drops the client_id/client_secret copies older token files carried.
    A failed write is logged, not raised: the credentials in hand still work.
    Returns the record as written (the base for the next refresh).
    """
    updated = {k: v for k, v in tokens.items() if k not in ("client_id", "client_secret")}
    updated["access_token"] = creds.token
    updated["expiry"] = _format_expiry(creds.expiry)
    if creds.refresh_token:
        updated["refresh_token"] = creds.refresh_token
    try:
        save_tokens(operator, updated)
    except OSError as e:
        print(f"[Google] Could not save refreshed token for {operator}: {e}")
    return updated


def probe_connection(operator: str) -> Dict[str, Any]:
    """Prove the operator's stored grant still works (one cheap users.getProfile).

    Blocking — callers on the event loop run it in a thread. Returns
    {"state", "email", "error", "error_message"} where state is one of:
      not_connected  no stored token
      connected      Google accepted the grant (email filled in)
      needs_reauth   the grant is dead; error is Google's code (invalid_grant, ...)
      unverified     Google could not be asked (network, 5xx); the grant may be fine
    """
    out: Dict[str, Any] = {"state": "not_connected", "email": "", "error": None, "error_message": None}
    try:
        if not is_connected(operator):
            return out
    except (OSError, ValueError) as e:
        out.update(state="needs_reauth", error="token_unreadable",
                   error_message=reauth_message(operator, "token_unreadable"))
        print(f"[Gmail] Token file for {operator} is unreadable: {e}")
        return out
    try:
        service = get_gmail_service(operator)
        profile = service.users().getProfile(userId="me").execute()
        out.update(state="connected", email=profile.get("emailAddress", "") or "")
    except Exception as e:
        code = reauth_error_code(e)
        if code:
            out.update(state="needs_reauth", error=code, error_message=reauth_message(operator, code))
        else:
            out.update(state="unverified", error="probe_failed",
                       error_message=f"Could not reach Google to verify the sign-in: {e}")
        print(f"[Gmail] Status probe for {operator}: {out['state']} ({e})")
    return out


def get_gmail_service(operator: str):
    """Build an authenticated Gmail API service for an operator.
    Returns None if not connected; raises GoogleReauthRequired for a dead grant.
    """
    creds = _get_credentials(operator)
    return build("gmail", "v1", credentials=creds, cache_discovery=False) if creds else None


def get_docs_service(operator: str):
    """Build an authenticated Google Docs API service for an operator."""
    creds = _get_credentials(operator)
    return build("docs", "v1", credentials=creds, cache_discovery=False) if creds else None


def get_sheets_service(operator: str):
    """Build an authenticated Google Sheets API service for an operator."""
    creds = _get_credentials(operator)
    return build("sheets", "v4", credentials=creds, cache_discovery=False) if creds else None


def get_slides_service(operator: str):
    """Build an authenticated Google Slides API service for an operator."""
    creds = _get_credentials(operator)
    return build("slides", "v1", credentials=creds, cache_discovery=False) if creds else None


def get_drive_service(operator: str):
    """Build an authenticated Google Drive API service for an operator."""
    creds = _get_credentials(operator)
    return build("drive", "v3", credentials=creds, cache_discovery=False) if creds else None


def get_calendar_service(operator: str):
    """Build an authenticated Google Calendar API service for an operator."""
    creds = _get_credentials(operator)
    return build("calendar", "v3", credentials=creds, cache_discovery=False) if creds else None


def list_messages(operator: str, query: str = "", max_results: int = 10) -> List[Dict]:
    """List/search Gmail messages. Returns list of message summaries."""
    service = get_gmail_service(operator)
    if not service:
        return [{"error": "Gmail not connected. Ask the operator to connect Gmail in the system menu."}]

    try:
        params = {"userId": "me", "maxResults": max_results}
        if query:
            params["q"] = query
        result = service.users().messages().list(**params).execute()
        messages = result.get("messages", [])

        summaries = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            summaries.append({
                "id": msg_ref["id"],
                "threadId": msg.get("threadId"),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
                "labelIds": msg.get("labelIds", []),
                "unread": "UNREAD" in msg.get("labelIds", [])
            })
        return summaries
    except Exception as e:
        return [{"error": _api_error("Gmail API error", operator, e)}]


def get_message(operator: str, message_id: str) -> Dict:
    """Get full email content by ID."""
    service = get_gmail_service(operator)
    if not service:
        return {"error": "Gmail not connected."}

    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

        # Extract body
        body = ""
        payload = msg.get("payload", {})
        if "body" in payload and payload["body"].get("data"):
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        elif "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    break
            if not body:
                for part in payload["parts"]:
                    if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                        body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                        break

        return {
            "id": msg["id"],
            "threadId": msg.get("threadId"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body[:5000],  # Cap at 5K chars
            "snippet": msg.get("snippet", ""),
            "labelIds": msg.get("labelIds", [])
        }
    except Exception as e:
        return {"error": _api_error("Gmail API error", operator, e)}


def send_email(operator: str, to: str, subject: str, body: str,
               cc: str = "", reply_to_message_id: str = "", thread_id: str = "") -> Dict:
    """Send an email or reply to a thread."""
    service = get_gmail_service(operator)
    if not service:
        return {"error": "Gmail not connected."}

    try:
        message = MIMEMultipart()
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if reply_to_message_id:
            message["In-Reply-To"] = reply_to_message_id
            message["References"] = reply_to_message_id
        message.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        send_body = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        result = service.users().messages().send(userId="me", body=send_body).execute()
        return {"success": True, "messageId": result.get("id"), "threadId": result.get("threadId")}
    except Exception as e:
        return {"error": _api_error("Send failed", operator, e)}


def get_labels(operator: str) -> List[Dict]:
    """Get all Gmail labels for an operator."""
    service = get_gmail_service(operator)
    if not service:
        return [{"error": "Gmail not connected."}]

    try:
        result = service.users().labels().list(userId="me").execute()
        return [{"id": l["id"], "name": l["name"], "type": l.get("type", "")}
                for l in result.get("labels", [])]
    except Exception as e:
        return [{"error": _api_error("Gmail API error", operator, e)}]


def modify_message(operator: str, message_id: str, add_labels: List[str] = None,
                   remove_labels: List[str] = None) -> Dict:
    """Modify labels on a message (mark read/unread, archive, star, etc)."""
    service = get_gmail_service(operator)
    if not service:
        return {"error": "Gmail not connected."}

    try:
        body = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels
        result = service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return {"success": True, "labelIds": result.get("labelIds", [])}
    except Exception as e:
        return {"error": _api_error("Gmail API error", operator, e)}
