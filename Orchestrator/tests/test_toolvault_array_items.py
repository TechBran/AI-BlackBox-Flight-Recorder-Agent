"""P1.2 — every declared array parameter carries an `items` schema.

Root cause 2026-07-11: update_sheet_values declared values as array-of-array
with NO inner items; Google's BidiGenerateContent setup validator rejected the
ENTIRE 56-tool setup with WS close 1007, killing every Gemini Live session
since 2026-06-20 (ff43d8b). This rule turns that class of regression into a
CI failure instead of a silent voice outage.
"""
from Orchestrator.toolvault import validate


def _tool(params: dict) -> dict:
    return {"name": "t", "parameters": params}


def test_outer_array_without_items_is_flagged():
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {"xs": {"type": "array"}},
    }))
    assert len(errors) == 1
    assert "parameters.properties.xs" in errors[0]


def test_2d_array_missing_inner_items_is_flagged():
    # The EXACT pre-fix update_sheet_values shape.
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "array"}},
        },
    }))
    assert len(errors) == 1
    assert "parameters.properties.values.items" in errors[0]


def test_complete_2d_array_is_clean():
    errors = validate._array_items_errors(_tool({
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
    }))
    assert errors == []


def test_non_dict_and_missing_parameters_never_raise():
    assert validate._array_items_errors(None) == []
    assert validate._array_items_errors({"name": "t"}) == []
    assert validate._array_items_errors({"name": "t", "parameters": "nope"}) == []


def test_all_toolvault_modules_declare_array_items():
    """Regression scan over EVERY real module folder (the CI gate)."""
    report = validate.validate_all()
    offenders = {
        folder: [m for m in msgs if "lacks required 'items'" in m]
        for folder, msgs in report["errors"].items()
    }
    offenders = {f: msgs for f, msgs in offenders.items() if msgs}
    assert offenders == {}, f"array params missing items: {offenders}"
