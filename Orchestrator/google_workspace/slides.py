"""Google Slides helpers — per-operator create / read / raw batchUpdate.

Each helper takes `(operator, ...)`, builds an authenticated Slides v1 service via
`Orchestrator.gmail.service.get_slides_service`, wraps the Google call in
try/except `googleapiclient.errors.HttpError`, and returns a plain dict (errors
under the "error" key) so the tool executors stay thin.

`slides_batch_update` is a deliberate RAW passthrough: the caller supplies the
Google Slides API `requests` array verbatim, giving the model full structural
editing (createSlide / insertText / createShape / deleteObject / etc.) without
us re-modelling the entire Slides request surface.

A 403 (insufficient scopes) is special-cased into a customer-facing "reconnect"
message because it happens until the user re-consents to the Slides scope.
"""

from googleapiclient.errors import HttpError

from Orchestrator.gmail.service import get_slides_service


def _err(msg):
    return {"error": msg}


def _http_err(e):
    """Map an HttpError to a customer-facing error dict.

    403 = the operator's stored token predates the Slides scope; the only fix is
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
            "Google Workspace needs reconnect to grant Slides access "
            "(re-run the Google sign-in in onboarding)"
        )
    reason = getattr(e, "reason", None) or str(e)
    return _err(f"Google Slides API error: {reason} (status {status})")


def _element_text(element):
    """Concatenate the text runs of a pageElement's shape, if any."""
    shape = element.get("shape") or {}
    text = shape.get("text") or {}
    parts = []
    for te in text.get("textElements", []) or []:
        run = te.get("textRun")
        if run and run.get("content") is not None:
            parts.append(run["content"])
    return "".join(parts)


def _element_type(element):
    """Classify a pageElement by which structural key it carries."""
    for key in ("shape", "image", "video", "line", "table",
                "wordArt", "sheetsChart", "elementGroup"):
        if key in element:
            return key
    return "other"


def create_presentation(operator, title):
    """Create a new Google Slides presentation.

    Returns {presentation_id, title, url} or {"error": ...}.
    """
    svc = get_slides_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        pres = svc.presentations().create(body={"title": title}).execute()
        pres_id = pres["presentationId"]
        return {
            "presentation_id": pres_id,
            "title": title,
            "url": f"https://docs.google.com/presentation/d/{pres_id}/edit",
        }
    except HttpError as e:
        return _http_err(e)


def read_presentation(operator, presentation_id):
    """Read a presentation's slides with their objectIds + pageElements.

    Returns a structured slide list so the model can target `slides_batch_update`
    edits. Each slide carries its objectId and its pageElements (each with
    object_id, type, and any text):
        {presentation_id, title, slides: [
            {object_id, page_elements: [{object_id, type, text?}]}
        ]}
    or {"error": ...}.
    """
    svc = get_slides_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        pres = svc.presentations().get(presentationId=presentation_id).execute()
        slides_out = []
        for slide in pres.get("slides", []) or []:
            elements_out = []
            for el in slide.get("pageElements", []) or []:
                entry = {
                    "object_id": el.get("objectId"),
                    "type": _element_type(el),
                }
                text = _element_text(el)
                if text:
                    entry["text"] = text
                elements_out.append(entry)
            slides_out.append({
                "object_id": slide.get("objectId"),
                "page_elements": elements_out,
            })
        return {
            "presentation_id": pres.get("presentationId", presentation_id),
            "title": pres.get("title", ""),
            "slides": slides_out,
        }
    except HttpError as e:
        return _http_err(e)


def slides_batch_update(operator, presentation_id, requests):
    """Apply a RAW Google Slides API `requests` array to a presentation, verbatim.

    `requests` is passed straight through to presentations().batchUpdate under
    body={"requests": requests} — full structural editing surface, no re-modelling.
    Returns {presentation_id, replies, applied} or {"error": ...}.
    """
    svc = get_slides_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        result = svc.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute()
        return {
            "presentation_id": presentation_id,
            "replies": result.get("replies", []),
            "applied": len(requests) if isinstance(requests, list) else None,
        }
    except HttpError as e:
        return _http_err(e)
