"""Tests for the ToolVault v2 module schema validator (Task 0.2).

validate_module_dict(d, folder_name, known_sources) returns a list of
human-readable error strings ([] == valid) and never raises on malformed input.
"""

from Orchestrator.toolvault.schema_spec import validate_module_dict


def _valid():
    """A known-good schema dict for the 'send_sms' tool."""
    return {
        "name": "send_sms",
        "description": "Send an SMS message to a phone number.",
        "category": "communication",
        "groups": ["chat", "phone", "mcp"],
        "tier": 2,
        "executor": "send_text",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient phone number"},
                "operator": {
                    "type": "string",
                    "x-source": "operators",
                    "description": "Operator sending the message",
                },
            },
            "required": ["to"],
        },
        "returns": "Delivery status",
        "example": 'send_sms(to="+15551234567")',
        "notes": "",
    }


def test_valid_schema_returns_empty_list():
    assert validate_module_dict(_valid(), "send_sms", known_sources={"operators"}) == []


def test_returns_list_of_strings():
    d = _valid()
    del d["name"]
    errors = validate_module_dict(d, "send_sms", known_sources={"operators"})
    assert isinstance(errors, list)
    assert all(isinstance(e, str) for e in errors)


def test_missing_name():
    d = _valid()
    del d["name"]
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_name_mismatch_folder():
    d = _valid()  # name == "send_sms"
    assert validate_module_dict(d, "send_text", known_sources={"operators"})


def test_missing_description():
    d = _valid()
    del d["description"]
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_missing_parameters():
    d = _valid()
    del d["parameters"]
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_parameters_not_object_schema_string_type():
    d = _valid()
    d["parameters"] = {"type": "string"}
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_parameters_not_a_dict():
    d = _valid()
    d["parameters"] = []
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_tier_out_of_range():
    d = _valid()
    d["tier"] = 4
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_tier_missing():
    d = _valid()
    del d["tier"]
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_group_not_in_known_set():
    d = _valid()
    d["groups"] = ["chat", "bogus"]
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_unknown_x_source():
    d = _valid()
    d["parameters"]["properties"]["operator"]["x-source"] = "nonexistent"
    assert validate_module_dict(d, "send_sms", known_sources={"operators"})


def test_known_x_source_is_ok():
    d = _valid()
    # operator property already carries x-source "operators"
    assert validate_module_dict(d, "send_sms", known_sources={"operators"}) == []


def test_parameters_none_no_exception():
    d = _valid()
    d["parameters"] = None
    errors = validate_module_dict(d, "send_sms", known_sources={"operators"})
    assert isinstance(errors, list)
    assert errors  # non-empty


def test_parameters_empty_list_no_exception():
    d = _valid()
    d["parameters"] = []
    errors = validate_module_dict(d, "send_sms", known_sources={"operators"})
    assert isinstance(errors, list)
    assert errors
