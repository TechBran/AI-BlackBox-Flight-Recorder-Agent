"""Operator-name validation for names that arrive from a client.

An operator name is joined into file paths (artifact ids, the Chrome profile
dir), written into the ledger (``OPERATOR: <name>`` in every snapshot body) and
persisted into config.ini's comma-separated ``[users] list``. A name arriving in
a request body must therefore be refused when it could steer a path, forge a
ledger line or split the roster -- BEFORE any of that happens.

Syntactic only, by design: there is no roster-membership check here. ``system``
is a legitimate operator for apps, MCP and background paths without being in
/operators, and /operator/add exists to create new names. Internal spaces are
allowed (the reserved 'Flight Recorder' has one; rosters carry 'B2 Tester').

Stdlib only, like utils.paths, so lean processes can import it.
"""
from __future__ import annotations

import unicodedata

MAX_OPERATOR_LEN = 64

# Unicode categories that never belong in a name: control (newlines, tabs,
# NUL), format (bidi overrides, zero-width), surrogates, private use and
# unassigned code points, and the line/paragraph separators.
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
# '/' and '\\' steer paths; ',' splits config.ini's roster list.
_FORBIDDEN_CHARS = {"/", "\\", ","}


class InvalidOperatorName(ValueError):
    """An operator name that is unsafe to use in a path, the ledger or config."""


def validate_operator(name) -> str:
    """Return ``name`` unchanged if it is a safe operator name, else raise.

    Refused: non-strings, empty or whitespace-only names, leading/trailing
    whitespace, names over :data:`MAX_OPERATOR_LEN` characters, ``/``, ``\\``,
    ``,``, any ``..``, the bare name ``.``, control/format characters (NUL,
    newline, tab, bidi overrides, ...) and any whitespace other than a plain
    internal space.
    """
    if not isinstance(name, str):
        raise InvalidOperatorName(f"operator must be a string, got {type(name).__name__}")
    if not name or not name.strip():
        raise InvalidOperatorName("operator name is empty")
    if name != name.strip():
        raise InvalidOperatorName("operator name has leading or trailing whitespace")
    if len(name) > MAX_OPERATOR_LEN:
        raise InvalidOperatorName(f"operator name is longer than {MAX_OPERATOR_LEN} characters")
    if name == "." or ".." in name:
        raise InvalidOperatorName("operator name may not contain '..' or be '.'")
    for ch in name:
        if ch in _FORBIDDEN_CHARS:
            raise InvalidOperatorName(f"operator name may not contain {ch!r}")
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES or (ch.isspace() and ch != " "):
            raise InvalidOperatorName(f"operator name may not contain {ch!r}")
    return name
