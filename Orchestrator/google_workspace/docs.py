"""Google Docs helpers — per-operator create / read / raw batchUpdate.

Each helper takes `(operator, ...)`, builds an authenticated Docs v1 service via
`Orchestrator.gmail.service.get_docs_service`, wraps the Google call in
try/except `googleapiclient.errors.HttpError`, and returns a plain dict (errors
under the "error" key) so the tool executors stay thin.

`docs_batch_update` is a deliberate RAW passthrough: the caller supplies the
Google Docs API `requests` array verbatim, giving the model full structural
editing (insertText / updateTextStyle / insertTable / replaceAllText / etc.)
without us re-modelling the entire Docs request surface.

A 403 (insufficient scopes) is special-cased into a customer-facing "reconnect"
message because it happens until the user re-consents to the Docs scope.
"""

from googleapiclient.errors import HttpError

from Orchestrator.gmail.service import get_docs_service


def _err(msg):
    return {"error": msg}


def _http_err(e):
    """Map an HttpError to a customer-facing error dict.

    403 = the operator's stored token predates the Docs scope; the only fix is
    to re-run the Google sign-in so the new scope is granted.
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
        return _err(
            "Google Workspace needs reconnect to grant Docs access "
            "(re-run the Google sign-in in onboarding)"
        )
    reason = getattr(e, "reason", None) or str(e)
    return _err(f"Google Docs API error: {reason} (status {status})")


def create_doc(operator, title, text=None):
    """Create a new Google Doc (optionally seeding it with `text`).

    Returns {document_id, title, url} or {"error": ...}.
    """
    svc = get_docs_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        doc = svc.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]
        if text:
            svc.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [
                    {"insertText": {"location": {"index": 1}, "text": text}}
                ]},
            ).execute()
        return {
            "document_id": doc_id,
            "title": title,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
    except HttpError as e:
        return _http_err(e)


def read_doc(operator, document_id):
    """Read a Google Doc's plain text + a structured element index map.

    The element list mirrors the document's structural elements with their
    `startIndex`/`endIndex` (and any object/element IDs) so the model can target
    `docs_batch_update` edits precisely. Returns:
        {document_id, title, text, elements: [...], inline_object_ids: [...]}
    or {"error": ...}.
    """
    svc = get_docs_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        doc = svc.documents().get(documentId=document_id).execute()
        body = doc.get("body", {}) or {}
        content = body.get("content", []) or []

        text_parts = []
        elements = []
        for struct in content:
            entry = {
                "startIndex": struct.get("startIndex"),
                "endIndex": struct.get("endIndex"),
            }
            elem_text_parts = []

            if "paragraph" in struct:
                para = struct["paragraph"]
                entry["type"] = "paragraph"
                style = (para.get("paragraphStyle") or {}).get("namedStyleType")
                if style:
                    entry["namedStyleType"] = style
                for el in para.get("elements", []) or []:
                    run = el.get("textRun")
                    if run and run.get("content") is not None:
                        elem_text_parts.append(run["content"])
                    # Surface any embedded object IDs the model can target.
                    for key in ("inlineObjectElement", "footnoteReference",
                                "horizontalRule", "pageBreak"):
                        obj = el.get(key)
                        if isinstance(obj, dict):
                            oid = obj.get("inlineObjectId") or obj.get("footnoteId")
                            if oid:
                                entry.setdefault("object_ids", []).append(oid)
            elif "table" in struct:
                entry["type"] = "table"
            elif "tableOfContents" in struct:
                entry["type"] = "tableOfContents"
            elif "sectionBreak" in struct:
                entry["type"] = "sectionBreak"
            else:
                entry["type"] = "other"

            elem_text = "".join(elem_text_parts)
            entry["text"] = elem_text
            text_parts.append(elem_text)
            elements.append(entry)

        return {
            "document_id": doc.get("documentId", document_id),
            "title": doc.get("title", ""),
            "text": "".join(text_parts),
            "elements": elements,
            "inline_object_ids": list((doc.get("inlineObjects") or {}).keys()),
            "named_range_ids": list((doc.get("namedRanges") or {}).keys()),
        }
    except HttpError as e:
        return _http_err(e)


def docs_batch_update(operator, document_id, requests):
    """Apply a RAW Google Docs API `requests` array to a document, verbatim.

    `requests` is passed straight through to documents().batchUpdate under
    body={"requests": requests} — full structural editing surface, no re-modelling.
    Returns the API response (with document_id) or {"error": ...}.
    """
    svc = get_docs_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        result = svc.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()
        return {
            "document_id": document_id,
            "replies": result.get("replies", []),
            "applied": len(requests) if isinstance(requests, list) else None,
        }
    except HttpError as e:
        return _http_err(e)
