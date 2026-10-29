"""ToolVault v2 — module schema validator.

A pure, dependency-free (stdlib only) validator for a single tool module's
``schema.json`` (already parsed into a dict). It returns a list of
human-readable error strings — empty list means the schema is valid — and is
defensive: malformed input yields errors, never an exception.

The set of known ``x-source`` resolver names is passed in as ``known_sources``
to avoid an import cycle with the resolvers module (built in a later task).

It also carries the three guards that used to run only in ``validate.py`` (the
CI gate): an unknown ``x-availability.feature``, a ``cli_agent`` tool in the
public ``mcp`` group, and an array without ``items``. The registry validates
every module through here when it loads, so a schema edited on a running box
(picked up by the registry's mtime cache — no restart) is excluded at load time
instead of taking down injection, the voice sessions or the Funnel's catalog
(audit O17-toolvault-runtime-04).
"""

KNOWN_GROUPS = {"chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"}
VALID_TIERS = {1, 2, 3}
# Upper bound for an executor's ``timeout_s`` (seconds). A longer job belongs
# in a background task the model polls (get_task_status), not one tool call.
MAX_TIMEOUT_S = 3600
REQUIRED_KEYS = ("name", "description", "category", "groups", "tier", "parameters")


def validate_module_dict(d: dict, folder_name: str, known_sources: set,
                         known_features=None) -> list[str]:
    """Validate a single module's schema dict.

    Args:
        d: The parsed schema.json contents.
        folder_name: The tool's folder name; ``d["name"]`` must equal it.
        known_sources: Set of registered ``x-source`` resolver names.
        known_features: The ``x-availability.feature`` names the availability
            gate serves; None means ``availability.FEATURES``.

    Returns:
        A list of human-readable error strings; empty when the schema is valid.
        Never raises on malformed input.
    """
    errors: list[str] = []

    if not isinstance(d, dict):
        return [f"schema must be a JSON object (dict), got {type(d).__name__}"]

    if known_sources is None:
        known_sources = set()

    # Required keys present.
    for key in REQUIRED_KEYS:
        if key not in d:
            errors.append(f"missing required key: '{key}'")

    # name == folder_name.
    name = d.get("name")
    if "name" in d:
        if not isinstance(name, str):
            errors.append(f"'name' must be a string, got {type(name).__name__}")
        elif name != folder_name:
            errors.append(
                f"'name' ({name!r}) must match folder name ({folder_name!r})"
            )

    # description present + non-empty string.
    if "description" in d:
        desc = d.get("description")
        if not isinstance(desc, str) or not desc.strip():
            errors.append("'description' must be a non-empty string")

    # category present + non-empty string.
    if "category" in d:
        cat = d.get("category")
        if not isinstance(cat, str) or not cat.strip():
            errors.append("'category' must be a non-empty string")

    # groups is a list & every entry in KNOWN_GROUPS.
    if "groups" in d:
        groups = d.get("groups")
        if not isinstance(groups, list):
            errors.append(
                f"'groups' must be a list, got {type(groups).__name__}"
            )
        else:
            for g in groups:
                if g not in KNOWN_GROUPS:
                    errors.append(
                        f"unknown group {g!r}; known groups: "
                        f"{sorted(KNOWN_GROUPS)}"
                    )

    # tier in VALID_TIERS.
    if "tier" in d:
        tier = d.get("tier")
        if tier not in VALID_TIERS:
            errors.append(
                f"'tier' must be one of {sorted(VALID_TIERS)}, got {tier!r}"
            )

    # parameters: dict with type=="object" and a dict properties; check x-source.
    if "parameters" in d:
        errors.extend(_validate_parameters(d.get("parameters"), known_sources))

    # Optional executor: if present, must be a non-empty string.
    if "executor" in d:
        ex = d.get("executor")
        if not isinstance(ex, str) or not ex.strip():
            errors.append("'executor' (optional) must be a non-empty string when present")

    # Optional timeout_s: how long the dispatcher waits for this tool's
    # executor (a positive number of seconds; bool is not a number here).
    if "timeout_s" in d:
        t = d.get("timeout_s")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= MAX_TIMEOUT_S:
            errors.append(
                f"'timeout_s' (optional) must be a number of seconds in "
                f"(0, {MAX_TIMEOUT_S}], got {t!r}"
            )

    # Optional x-availability: presence-gate metadata consumed by
    # availability.is_available (provider key + enabled-pref). When present it
    # must be an object with a non-empty string "provider" and, if given, a
    # "requires_env" list of strings.
    if "x-availability" in d:
        errors.extend(_validate_x_availability(d.get("x-availability")))

    if known_features is None:
        from . import availability  # stdlib only; no cycle back to this module
        known_features = availability.FEATURES
    errors.extend(x_availability_feature_errors(d, known_features))
    errors.extend(cli_agent_mcp_group_errors(d))
    errors.extend(array_items_errors(d))

    return errors


def _validate_parameters(params, known_sources: set) -> list[str]:
    """Validate the JSON-Schema ``parameters`` object and any ``x-source`` markers."""
    errors: list[str] = []

    if not isinstance(params, dict):
        return [
            f"'parameters' must be a JSON-Schema object (dict), got "
            f"{type(params).__name__}"
        ]

    if params.get("type") != "object":
        errors.append(
            f"'parameters.type' must be 'object', got {params.get('type')!r}"
        )

    properties = params.get("properties")
    if not isinstance(properties, dict):
        errors.append(
            f"'parameters.properties' must be a dict, got "
            f"{type(properties).__name__}"
        )
        return errors

    # required (optional) must be a list if present.
    if "required" in params and not isinstance(params.get("required"), list):
        errors.append(
            f"'parameters.required' must be a list, got "
            f"{type(params.get('required')).__name__}"
        )

    for prop_name, prop in properties.items():
        if not isinstance(prop, dict):
            errors.append(
                f"property {prop_name!r} must be a dict, got {type(prop).__name__}"
            )
            continue
        if "x-source" in prop:
            src = prop.get("x-source")
            if src not in known_sources:
                errors.append(
                    f"property {prop_name!r} has unknown x-source {src!r}; "
                    f"known sources: {sorted(known_sources)}"
                )

    return errors


def _validate_x_availability(gate) -> list[str]:
    """Validate an optional top-level ``x-availability`` presence-gate object."""
    errors: list[str] = []
    if not isinstance(gate, dict):
        return [
            f"'x-availability' (optional) must be an object (dict), got "
            f"{type(gate).__name__}"
        ]
    provider = gate.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        errors.append("'x-availability.provider' must be a non-empty string")
    if "requires_env" in gate:
        req = gate.get("requires_env")
        if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
            errors.append(
                "'x-availability.requires_env' (optional) must be a list of strings"
            )
    return errors


def x_availability_feature_errors(data, known_features) -> list:
    """Guard: an ``x-availability.feature``, when present, MUST be one the
    availability gate serves (a key of ``availability.FEATURES``).

    An unknown feature is not a structural error, it is a runtime landmine: the
    gate has no provider map for it, so before this guard ran at load time a
    bogus feature raised KeyError inside ``filter_available`` and took down the
    live injector AND made the MCP ``list_tools`` return ZERO tools.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict):
        return []
    gate = data.get("x-availability")
    if not isinstance(gate, dict) or "feature" not in gate:
        return []
    feature = gate.get("feature")
    known = set(known_features or ())
    if isinstance(feature, str) and feature in known:
        return []
    return [
        f"x-availability.feature {feature!r} is not a known availability.FEATURES "
        f"key; known features: {sorted(known)}"
    ]


def cli_agent_mcp_group_errors(data) -> list:
    """Guard the D1 invariant for EVERY tool, not just the three known names: a
    tool categorized ``cli_agent`` must NEVER be in the ``mcp`` group.

    The ``mcp`` group feeds ``MCP/blackbox_mcp_server.py``, exposed over the
    Tailscale **Funnel** (public HTTPS ingress). A fully-open CLI-agent tool there
    is remote arbitrary command execution under the operator's shell. A test
    parametrized over today's tool names can't catch a 4th cli_agent tool added
    later with ``mcp`` in its groups — a category-keyed check can, and does.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict) or data.get("category") != "cli_agent":
        return []
    groups = data.get("groups")
    if isinstance(groups, list) and "mcp" in groups:
        name = data.get("name", "<unknown>")
        return [
            f"cli_agent tool {name!r} must NOT be in the 'mcp' group: the mcp "
            f"group is served over the public Tailscale Funnel, which would make a "
            f"fully-open CLI agent remote arbitrary command execution (invariant D1)."
        ]
    return []


def array_items_errors(data) -> list:
    """Guard: every ``"type": "array"`` node anywhere in ``parameters`` MUST
    carry an ``items`` schema (a dict).

    Google's Gemini Live BidiGenerateContent setup validator rejects the ENTIRE
    multi-tool setup with WS close 1007 (``...items: missing field``) when any
    declared array lacks ``items`` — one bad tool kills every voice session for
    every operator, silently (2026-07-11 root cause: update_sheet_values' 2D
    ``values`` param, dead since 2026-06-20). This check walks the whole
    parameters tree (properties, items, nested anything) so N-dimensional
    arrays are covered.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict):
        return []
    params = data.get("parameters")
    if not isinstance(params, dict):
        return []
    errors: list = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "array" and not isinstance(node.get("items"), dict):
                errors.append(
                    f"array at {path} lacks required 'items' schema — Gemini Live "
                    f"rejects the whole tool setup with WS close 1007"
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(params, "parameters")
    return errors
