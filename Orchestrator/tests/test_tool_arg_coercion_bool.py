"""Tests for M2.3 — coerce stringified booleans at the tool-arg chokepoint.

Models sometimes emit boolean tool args as STRINGS (one_shot='false',
pause='false'). Left as-is, one_shot='false' is a truthy str → stored as 1
(the job auto-deletes); pause='false' pauses on a resume. The existing
_coerce_stringified_json_args chokepoint already normalizes array/object JSON
strings; this extends it to schema-declared boolean params so ALL tools
benefit. create_cron_job.one_shot is declared type "boolean".
"""

import pytest

from Orchestrator.tools.blackbox_tools import _coerce_stringified_json_args


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("TRUE", True),
        ("1", True), ("yes", True), ("YES", True),
        ("on", True), ("On", True),
        ("false", False), ("False", False), ("FALSE", False),
        ("0", False), ("no", False), ("NO", False),
        ("off", False), ("Off", False), ("", False),
    ],
)
def test_stringified_boolean_coerced(raw, expected):
    # create_cron_job.one_shot is declared type "boolean".
    out = _coerce_stringified_json_args(
        "create_cron_job",
        {"name": "n", "prompt": "p", "schedule": "0 7 * * *", "one_shot": raw},
    )
    assert out["one_shot"] is expected, f"{raw!r} -> {out['one_shot']!r}"


def test_false_string_does_not_become_truthy():
    """The headline bug: one_shot='false' must NOT be stored truthy."""
    out = _coerce_stringified_json_args(
        "create_cron_job",
        {"name": "n", "prompt": "p", "schedule": "0 7 * * *", "one_shot": "false"},
    )
    assert out["one_shot"] is False


def test_real_bool_passes_through_unchanged():
    src = {"name": "n", "prompt": "p", "schedule": "0 7 * * *", "one_shot": True}
    out = _coerce_stringified_json_args("create_cron_job", src)
    assert out["one_shot"] is True

    src2 = {"name": "n", "prompt": "p", "schedule": "0 7 * * *", "one_shot": False}
    out2 = _coerce_stringified_json_args("create_cron_job", src2)
    assert out2["one_shot"] is False


def test_non_boolean_typed_param_untouched():
    """A schema string param that happens to look boolean is left alone."""
    out = _coerce_stringified_json_args(
        "create_cron_job",
        {"name": "true", "prompt": "p", "schedule": "0 7 * * *"},
    )
    # 'name' is type "string"; must remain the literal string "true".
    assert out["name"] == "true"


def test_unrecognized_string_for_boolean_param_left_as_is():
    """An ambiguous value (not in the known true/false vocab) is not coerced,
    so existing validation can still reject it rather than silently guessing."""
    out = _coerce_stringified_json_args(
        "create_cron_job",
        {"name": "n", "prompt": "p", "schedule": "0 7 * * *", "one_shot": "maybe"},
    )
    assert out["one_shot"] == "maybe"


def test_array_object_coercion_still_works():
    """Extending boolean handling must not disturb array/object coercion."""
    import json
    payload = [{"insertText": {"text": "hi"}}]
    out = _coerce_stringified_json_args(
        "docs_batch_update",
        {"document_id": "d", "requests": json.dumps(payload)},
    )
    assert out["requests"] == payload
