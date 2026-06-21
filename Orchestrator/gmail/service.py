"""Gmail API service — per-operator OAuth token management + API calls."""

import base64
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Dict, List, Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from Orchestrator.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/calendar",
]
TOKEN_DIR = Path("Manifest/gmail_tokens")
TOKEN_DIR.mkdir(parents=True, exist_ok=True)


def _token_path(operator: str) -> Path:
    """Get token file path for an operator."""
    safe_name = operator.replace("/", "_").replace("\\", "_")
    return TOKEN_DIR / f"{safe_name}.json"


def save_tokens(operator: str, token_data: dict):
    """Save OAuth tokens to disk for an operator."""
    path = _token_path(operator)
    path.write_text(json.dumps(token_data, indent=2))


def load_tokens(operator: str) -> Optional[dict]:
    """Load OAuth tokens from disk for an operator."""
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
    """Check if an operator has valid Gmail tokens."""
    tokens = load_tokens(operator)
    return tokens is not None and "refresh_token" in tokens


def workspace_connected(operator: str) -> bool:
    """True iff a stored token with a refresh_token exists for the operator.
    The Workspace tools use this to return a clear "connect Google" message.
    """
    return is_connected(operator)


def _get_credentials(operator: str) -> Optional[Credentials]:
    """Build (and refresh if needed) shared OAuth credentials for an operator.
    Returns None if no token, no refresh_token, or the refresh fails.
    These credentials are scoped for Gmail + Docs/Sheets/Slides/Drive/Calendar.
    """
    tokens = load_tokens(operator)
    if not tokens or "refresh_token" not in tokens:
        return None

    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES
    )

    # Refresh if expired
    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
            # Save refreshed tokens
            tokens["access_token"] = creds.token
            save_tokens(operator, tokens)
        except Exception as e:
            print(f"[Google] Token refresh failed for {operator}: {e}")
            return None

    return creds


def get_gmail_service(operator: str):
    """Build an authenticated Gmail API service for an operator.
    Returns None if not connected or tokens are invalid.
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
        return [{"error": f"Gmail API error: {str(e)}"}]


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
        return {"error": f"Gmail API error: {str(e)}"}


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
        return {"error": f"Send failed: {str(e)}"}


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
        return [{"error": f"Gmail API error: {str(e)}"}]


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
        return {"error": f"Gmail API error: {str(e)}"}
