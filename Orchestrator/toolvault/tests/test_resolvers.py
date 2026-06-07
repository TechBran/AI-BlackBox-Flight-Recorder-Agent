"""Tests for the ToolVault v2 x-source resolver registry (Task 0.3).

The real ``operators`` resolver lazy-imports the backend operator list. These
tests NEVER touch the backend: they monkeypatch ``resolvers._list_operators``
to return a fixed list, so resolution is exercised deterministically offline.
"""

import copy

from Orchestrator.toolvault import resolvers
from Orchestrator.toolvault.context import ToolContext


def _schema_with_operators_source() -> dict:
    return {
        "type": "object",
        "properties": {
            "operator": {
                "type": "string",
                "description": "Whose work to record.",
                "x-source": "operators",
            },
            "message": {
                "type": "string",
                "description": "Plain field, no resolution.",
            },
        },
        "required": ["operator"],
    }


def test_known_sources_includes_operators():
    assert "operators" in resolvers.KNOWN_SOURCES
    # KNOWN_SOURCES is derived from RESOLVERS.
    assert resolvers.KNOWN_SOURCES == set(resolvers.RESOLVERS)


def test_resolve_operators_fills_enum_and_strips_x_source(monkeypatch):
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])
    schema = {"parameters": _schema_with_operators_source()}

    out = resolvers.resolve_schema(schema, ToolContext(operator="system"))

    op_prop = out["parameters"]["properties"]["operator"]
    assert op_prop["enum"] == ["Brandon", "system"]
    assert "x-source" not in op_prop
    # Other property keys are preserved.
    assert op_prop["type"] == "string"
    assert op_prop["description"] == "Whose work to record."


def test_resolve_does_not_mutate_input(monkeypatch):
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])
    schema = {"parameters": _schema_with_operators_source()}
    snapshot = copy.deepcopy(schema)

    out = resolvers.resolve_schema(schema, ToolContext())

    # Input untouched: still has x-source, still has no enum.
    assert schema == snapshot
    in_op = schema["parameters"]["properties"]["operator"]
    assert in_op["x-source"] == "operators"
    assert "enum" not in in_op
    # And the output is a different object.
    assert out is not schema


def test_property_without_x_source_is_untouched(monkeypatch):
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])
    schema = {"parameters": _schema_with_operators_source()}

    out = resolvers.resolve_schema(schema, ToolContext())

    msg_prop = out["parameters"]["properties"]["message"]
    assert msg_prop == {"type": "string", "description": "Plain field, no resolution."}
    assert "enum" not in msg_prop


def test_unknown_x_source_does_not_raise_and_leaves_property(monkeypatch):
    # Even if _list_operators were callable, the unknown source must not invoke it.
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon"])
    schema = {
        "parameters": {
            "type": "object",
            "properties": {
                "weird": {
                    "type": "string",
                    "x-source": "nope",
                },
            },
        }
    }

    out = resolvers.resolve_schema(schema, ToolContext())

    weird = out["parameters"]["properties"]["weird"]
    # Property still present and unchanged (still has its x-source — left as-is).
    assert weird == {"type": "string", "x-source": "nope"}


def test_schema_without_parameters_returns_deep_copy(monkeypatch):
    schema = {"name": "thing", "description": "no params here"}

    out = resolvers.resolve_schema(schema, ToolContext())

    assert out == schema
    assert out is not schema


def test_schema_with_parameters_but_no_properties_is_graceful():
    schema = {"parameters": {"type": "object"}}

    out = resolvers.resolve_schema(schema, ToolContext())

    assert out == schema
    assert out is not schema
