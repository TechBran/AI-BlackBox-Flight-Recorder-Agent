"""ToolVault v2 — module schema validator.

A pure, dependency-free (stdlib only) validator for a single tool module's
``schema.json`` (already parsed into a dict). It returns a list of
human-readable error strings — empty list means the schema is valid — and is
defensive: malformed input yields errors, never an exception.

The set of known ``x-source`` resolver names is passed in as ``known_sources``
to avoid an import cycle with the resolvers module (built in a later task).
"""

KNOWN_GROUPS = {"chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"}
VALID_TIERS = {1, 2, 3}
REQUIRED_KEYS = ("name", "description", "category", "groups", "tier", "parameters")


def validate_module_dict(d: dict, folder_name: str, known_sources: set) -> list[str]:
    """Validate a single module's schema dict.

    Args:
        d: The parsed schema.json contents.
        folder_name: The tool's folder name; ``d["name"]`` must equal it.
        known_sources: Set of registered ``x-source`` resolver names.

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

    # Optional x-availability: presence-gate metadata consumed by
    # availability.is_available (provider key + enabled-pref). When present it
    # must be an object with a non-empty string "provider" and, if given, a
    # "requires_env" list of strings.
    if "x-availability" in d:
        errors.extend(_validate_x_availability(d.get("x-availability")))

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
