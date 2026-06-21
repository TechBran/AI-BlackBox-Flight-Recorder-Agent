"""Google Drive helpers — per-operator search / read / create / delete (any file type).

Each helper takes `(operator, ...)`, builds an authenticated Drive v3 service via
`Orchestrator.gmail.service.get_drive_service`, wraps the Google call in
try/except `googleapiclient.errors.HttpError`, and returns a plain dict (errors
under the "error" key) so the tool executors stay thin.

Unlike the Docs/Sheets/Slides helpers, Drive spans every file type: search by an
optional Drive query string, read metadata + a best-effort text rendering of the
content (Google-native types are exported to text; binary types are returned only
if they decode as UTF-8, else a "note" explains the content was omitted), create
files with optional inline media, and delete by id.

A 403 (insufficient scopes) is special-cased into a customer-facing "reconnect"
message because it happens until the user re-consents to the Drive scope.
"""

from googleapiclient.errors import HttpError
from Orchestrator.google_workspace._errors import http_error_to_dict
from googleapiclient.http import MediaInMemoryUpload

from Orchestrator.gmail.service import get_drive_service


def _err(msg):
    return {"error": msg}


def _http_err(e):
    return http_error_to_dict(e, "Drive")


# Text export targets for Google-native (application/vnd.google-apps.*) types.
# Anything not in this map is not exported (metadata-only with a note).
_GOOGLE_APPS_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def search_drive_files(operator, query=None, page_size=20):
    """Search the operator's Drive (optionally filtered by a Drive query string).

    `query` is a raw Google Drive `q` expression (e.g. "name contains 'report'"
    or "mimeType='application/pdf'"); None lists recent files. Returns a list of
    file dicts {id, name, mimeType, modifiedTime, size, webViewLink} or, on
    failure, the standard error dict.
    """
    svc = get_drive_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        resp = svc.files().list(
            q=query,
            pageSize=page_size,
            fields="files(id,name,mimeType,modifiedTime,size,webViewLink)",
        ).execute()
        return resp.get("files", [])
    except HttpError as e:
        return _http_err(e)


def get_drive_file(operator, file_id):
    """Read a Drive file's metadata + a best-effort text rendering of its content.

    Always returns the metadata (id, name, mimeType, size, modifiedTime,
    webViewLink). Then attempts content:
      - Google-native types (mimeType starts with application/vnd.google-apps.)
        are exported to a sensible text type (document/presentation -> text/plain,
        spreadsheet -> text/csv); other native types skip export with a "note".
      - Binary/other types are fetched via get_media and returned as "content"
        ONLY if they decode as UTF-8; otherwise a "note" explains the content was
        omitted (binary).
    Returns {"error": ...} on failure.
    """
    svc = get_drive_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        meta = svc.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,webViewLink",
        ).execute()
        result = dict(meta)
        mime = meta.get("mimeType", "") or ""

        if mime.startswith("application/vnd.google-apps."):
            export_mime = _GOOGLE_APPS_EXPORT.get(mime)
            if export_mime is None:
                result["note"] = (
                    "Google-native file with no plain-text export; content omitted "
                    "(open via webViewLink)"
                )
                return result
            data = svc.files().export(
                fileId=file_id, mimeType=export_mime
            ).execute()
        else:
            data = svc.files().get_media(fileId=file_id).execute()

        if isinstance(data, str):
            result["content"] = data
            return result
        try:
            result["content"] = data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            result["note"] = "Binary content omitted (not UTF-8 text)"
        return result
    except HttpError as e:
        return _http_err(e)


def create_drive_file(operator, name, mime_type, content=None):
    """Create a Drive file, optionally seeding it with inline `content`.

    body = {"name": name, "mimeType": mime_type}. If `content` is provided it is
    uploaded as media (str content is UTF-8 encoded). Returns the created file
    dict {id, name, mimeType, webViewLink} or {"error": ...}.
    """
    svc = get_drive_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        body = {"name": name, "mimeType": mime_type}
        media_body = None
        if content is not None:
            raw = content.encode("utf-8") if isinstance(content, str) else content
            media_body = MediaInMemoryUpload(raw, mimetype=mime_type)
        created = svc.files().create(
            body=body,
            media_body=media_body,
            fields="id,name,mimeType,webViewLink",
        ).execute()
        return created
    except HttpError as e:
        return _http_err(e)


def delete_drive_file(operator, file_id):
    """Delete a Drive file by id. Returns {"deleted": True, "file_id": ...}."""
    svc = get_drive_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        svc.files().delete(fileId=file_id).execute()
        return {"deleted": True, "file_id": file_id}
    except HttpError as e:
        return _http_err(e)
