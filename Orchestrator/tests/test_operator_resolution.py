"""Operator resolution decision logic (2026-05-31)."""
from MCP.operator_resolution import choose_operator

def test_explicit_operator_wins():
    assert choose_operator("Anna", ["Brandon", "Anna"], "Brandon") == ("Anna", False)

def test_single_operator_auto():
    assert choose_operator(None, ["Brandon"], "Brandon") == ("Brandon", False)

def test_single_operator_auto_ignores_blank():
    assert choose_operator("", ["Anna"], "Anna") == ("Anna", False)

def test_multiple_needs_selection_resolves_to_default():
    assert choose_operator(None, ["Brandon", "Anna", "Sam"], "Anna") == ("Anna", True)

def test_multiple_default_missing_falls_back_to_first():
    assert choose_operator(None, ["Brandon", "Anna"], "") == ("Brandon", True)

def test_empty_list_uses_default_or_operator():
    assert choose_operator(None, [], "") == ("Operator", False)
    assert choose_operator(None, [], "Zed") == ("Zed", False)
