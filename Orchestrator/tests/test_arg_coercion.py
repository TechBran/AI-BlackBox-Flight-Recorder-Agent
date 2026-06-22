"""Tests for _coerce_stringified_json_args.

Models sometimes emit array/object tool params as JSON-encoded STRINGS
(e.g. requests="[{...}]" instead of requests=[{...}]). The dispatch chokepoint
parses these back when the tool schema declares the param as array/object.
This was the root cause of the Google batch_update / update_sheet_values
failures in the 2026-06-22 sweep (the model double-encoded the array).
"""

import json

from Orchestrator.tools.blackbox_tools import _coerce_stringified_json_args


def test_stringified_requests_array_parsed_to_list():
    # docs_batch_update.requests is declared type "array".
    payload = [{"insertText": {"location": {"index": 1}, "text": "hi"}}]
    out = _coerce_stringified_json_args(
        "docs_batch_update",
        {"document_id": "doc1", "requests": json.dumps(payload)},
    )
    assert isinstance(out["requests"], list)
    assert out["requests"] == payload
    # scalar string param is untouched
    assert out["document_id"] == "doc1"


def test_stringified_2d_values_parsed_to_list():
    # update_sheet_values.values is declared type "array" (2D).
    values = [["a", "b"], ["c", "d"]]
    out = _coerce_stringified_json_args(
        "update_sheet_values",
        {"spreadsheet_id": "s1", "range": "A1:B2", "values": json.dumps(values)},
    )
    assert out["values"] == values


def test_real_list_is_passed_through_unchanged():
    payload = [{"insertText": {"text": "hi"}}]
    src = {"document_id": "d", "requests": payload}
    out = _coerce_stringified_json_args("docs_batch_update", src)
    assert out["requests"] is payload  # not copied/re-parsed


def test_non_json_string_for_array_param_left_as_is():
    # A genuine non-JSON string must NOT be coerced; the executor still rejects it.
    src = {"document_id": "d", "requests": "not json at all"}
    out = _coerce_stringified_json_args("docs_batch_update", src)
    assert out["requests"] == "not json at all"


def test_scalar_string_param_never_touched():
    # create_doc.title is a string; a JSON-looking title must stay a string.
    out = _coerce_stringified_json_args("create_doc", {"title": "[not a list]"})
    assert out["title"] == "[not a list]"


def test_unknown_tool_returns_input_unchanged():
    src = {"requests": "[1,2,3]"}
    out = _coerce_stringified_json_args("no_such_tool_xyz", src)
    assert out is src  # unknown tool -> no schema -> untouched


def test_slides_requests_stringified_parsed():
    payload = [{"createSlide": {}}]
    out = _coerce_stringified_json_args(
        "slides_batch_update",
        {"presentation_id": "p1", "requests": json.dumps(payload)},
    )
    assert out["requests"] == payload
