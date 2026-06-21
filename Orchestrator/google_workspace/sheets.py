"""Google Sheets helpers — per-operator create / read / update / raw batchUpdate.

Each helper takes `(operator, ...)`, builds an authenticated Sheets v4 service via
`Orchestrator.gmail.service.get_sheets_service`, wraps the Google call in
try/except `googleapiclient.errors.HttpError`, and returns a plain dict (errors
under the "error" key) so the tool executors stay thin.

`sheets_batch_update` is a deliberate RAW passthrough: the caller supplies the
Google Sheets API `requests` array verbatim, giving the model full structural
editing (addSheet / repeatCell / updateCells / mergeCells / etc.) without us
re-modelling the entire Sheets request surface.

A 403 (insufficient scopes) is special-cased into a customer-facing "reconnect"
message because it happens until the user re-consents to the Sheets scope.
"""

from googleapiclient.errors import HttpError
from Orchestrator.google_workspace._errors import http_error_to_dict

from Orchestrator.gmail.service import get_sheets_service


def _err(msg):
    return {"error": msg}


def _http_err(e):
    return http_error_to_dict(e, "Sheets")


def create_spreadsheet(operator, title):
    """Create a new Google Spreadsheet.

    Returns {spreadsheet_id, title, url} or {"error": ...}.
    """
    svc = get_sheets_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        ss = svc.spreadsheets().create(
            body={"properties": {"title": title}}
        ).execute()
        ss_id = ss["spreadsheetId"]
        return {
            "spreadsheet_id": ss_id,
            "title": title,
            "url": f"https://docs.google.com/spreadsheets/d/{ss_id}/edit",
        }
    except HttpError as e:
        return _http_err(e)


def read_sheet(operator, spreadsheet_id, range=None):
    """Read a spreadsheet — either a range of values or the sheet metadata.

    If `range` is given: read that A1-notation range and return {range, values}.
    If `range` is None: return the spreadsheet's sheet metadata so the model
    knows what ranges exist — {sheets: [{title, rows, cols, sheet_id}, ...]}.
    Returns {"error": ...} on failure.
    """
    svc = get_sheets_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        if range:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range
            ).execute()
            return {
                "range": resp.get("range", range),
                "values": resp.get("values", []),
            }
        ss = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets_meta = []
        for sheet in ss.get("sheets", []) or []:
            props = sheet.get("properties", {}) or {}
            grid = props.get("gridProperties", {}) or {}
            sheets_meta.append({
                "title": props.get("title"),
                "rows": grid.get("rowCount"),
                "cols": grid.get("columnCount"),
                "sheet_id": props.get("sheetId"),
            })
        return {"sheets": sheets_meta}
    except HttpError as e:
        return _http_err(e)


def update_sheet_values(operator, spreadsheet_id, range, values):
    """Write `values` (a 2D array) into `range` with USER_ENTERED parsing.

    USER_ENTERED means strings are parsed as if typed in the UI (formulas,
    dates, numbers). Returns {updated_range, updated_cells} or {"error": ...}.
    """
    svc = get_sheets_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        resp = svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        return {
            "updated_range": resp.get("updatedRange"),
            "updated_cells": resp.get("updatedCells"),
        }
    except HttpError as e:
        return _http_err(e)


def sheets_batch_update(operator, spreadsheet_id, requests):
    """Apply a RAW Google Sheets API `requests` array to a spreadsheet, verbatim.

    `requests` is passed straight through to spreadsheets().batchUpdate under
    body={"requests": requests} — full structural editing surface, no re-modelling.
    Returns {spreadsheet_id, replies, applied} or {"error": ...}.
    """
    svc = get_sheets_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        result = svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        return {
            "spreadsheet_id": spreadsheet_id,
            "replies": result.get("replies", []),
            "applied": len(requests) if isinstance(requests, list) else None,
        }
    except HttpError as e:
        return _http_err(e)
