"""Computer-use model resolution primitives.

Model ids are PROVIDER FACTS (they change every release); model CLASSES are our
stable taxonomy. Schemas carry a class name; the concrete id is resolved here at
execution time against the live /models/computer-use catalog and the per-vendor
CU_MODEL_FILTERS capability gates.

Public API:
  * resolve_backend(model)           — id -> driver backend (anthropic default).
  * resolve_model_class(class_or_id) — stable class name (or concrete id) -> id.
  * resolve_cu_model(model)          — cron/CU-stream helper (gate-or-default).
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from Orchestrator.config import CU_MODEL_FILTERS, CU_MODEL_DEFAULT

logger = logging.getLogger(__name__)

# Default class for empty/omitted input. Classes are OUR stable taxonomy; the
# concrete id is resolved from the live catalog (never pinned here).
_DEFAULT_CLASS = "opus"

# Closed set of accepted class aliases -> (backend, id substring the class owns).
# The three Claude families share the anthropic backend, so each requires its
# family token; gemini/gpt each own their backend but still require the token
# (keeps the legacy openai `computer-use-preview` out of the `gpt` class — it
# is reachable only as a verbatim concrete id).
_CLASS_SPEC = {
    "opus":   ("anthropic", "opus"),
    "sonnet": ("anthropic", "sonnet"),
    "fable":  ("anthropic", "fable"),
    "gemini": ("google", "gemini"),
    "gpt":    ("openai", "gpt"),
}


def resolve_backend(model: str) -> str:
    model = (model or CU_MODEL_DEFAULT).strip()
    for backend, pattern in CU_MODEL_FILTERS.items():
        if re.match(pattern, model):
            return backend
    return "anthropic"  # unknown claude-adjacent ids and "" fall through here


def _gate_passes(model_id: str) -> bool:
    """True iff the id passes SOME vendor CU-capability gate (strict — no
    anthropic fallthrough like resolve_backend). This is the honest answer to
    'is this a CU-capable concrete id?'."""
    return any(re.match(p, model_id) for p in CU_MODEL_FILTERS.values())


def _is_preview(model_id: str) -> bool:
    """A DATED preview id (disfavored when a GA sibling exists in the class).

    Keys off a `-preview-` segment WITH a trailing part, e.g.
    `gemini-2.5-computer-use-preview-10-2025`. A bare `-preview` SUFFIX is not a
    dated preview — this deliberately does NOT flag OpenAI's legacy
    `computer-use-preview`, so the standing GA-over-preview rule can never
    delete a vendor whose only CU model is a preview.
    """
    return "-preview-" in model_id


def _version_core(model_id: str) -> Tuple[int, ...]:
    """The VERSION portion of an id, as an int tuple, stopping before any date.

    A numeric run of >= 4 digits is a date/year component (`2026`, `20251101`),
    not a version — collecting stops there so a dated snapshot and its rolling
    alias share the SAME core (`gpt-5.5-2026-04-23` and `gpt-5.5` -> (5, 5)).
    Two- and three-digit runs are real version parts and are kept (`gpt-5.12`
    -> (5, 12)).

    ASSUMES version components are < 4 digits. A hypothetical `gpt-5.1000` would
    lose its minor (-> (5,)) and rank below `gpt-5.5`. No vendor numbers versions
    that way; the date-exclusion determinism is worth the trade.
    """
    core: List[int] = []
    for run in re.findall(r"\d+", model_id):
        if len(run) >= 4:  # date/year — end of the version core
            break
        core.append(int(run))
    return tuple(core)


def _version_key(model_id: str) -> Tuple[int, Tuple[int, ...], int, str]:
    """Total, catalog-order-independent sort key for 'newest in class' (max wins):

      1. GA (not a dated preview) outranks a preview.
      2. Higher version CORE wins (dates excluded, so they never inflate it).
      3. Same version -> the ROLLING alias (evergreen production pointer) beats a
         pinned dated/named snapshot; the shorter id is the rolling one, so a
         larger `-len` wins (`gpt-5.6` over `gpt-5.6-sol`; `gpt-5.5` over
         `gpt-5.5-2026-04-23`).
      4. Final absolute tie-break on the id string itself, so DISTINCT ids never
         collide and the result never depends on the catalog's ordering.
    """
    return (
        0 if _is_preview(model_id) else 1,
        _version_core(model_id),
        -len(model_id),
        model_id,
    )


def resolve_model_class(
    class_or_id: Optional[str],
    catalog: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Resolve a stable CU model CLASS name (or a concrete id) to a concrete id.

    Rules, in order:
      1. A concrete id that passes its vendor capability gate is returned
         verbatim (works even when the catalog is briefly unavailable).
      2. A class alias (opus/sonnet/fable/gemini/gpt) -> the newest id of that
         class in the live catalog. "Newest" is deterministic (see _version_key):
         GA preferred over a dated preview; among the same version the rolling
         alias (gpt-5.6) is preferred over a pinned snapshot (gpt-5.6-sol,
         gpt-5.5-2026-04-23); a preview-only class still resolves to its preview.
      3. Empty / omitted -> the default class (opus).
      4. Anything else (unknown alias, or a known class with no catalog member)
         -> ValueError naming the classes the catalog can currently satisfy, so
         an LLM caller can retry with a valid class.

    `catalog` is the list of catalog model dicts (each {"id", "backend", ...}).
    When None it is sourced live from GET /models/computer-use.
    """
    normalized = (class_or_id or "").strip() or _DEFAULT_CLASS

    # Rule 1 — concrete, gate-passing id wins verbatim.
    if _gate_passes(normalized):
        return normalized

    if catalog is None:
        # Lazy import: dispatch.py must never import a routes module at load
        # time (circular import). Reuse the live catalog builder rather than
        # duplicating vendor-fetch/filter logic.
        from Orchestrator.routes.admin_routes import get_available_models
        catalog = get_available_models("computer-use").get("models", [])

    # Rule 2 — class alias -> newest concrete id of that class.
    key = normalized.lower()
    spec = _CLASS_SPEC.get(key)
    if spec is not None:
        backend, token = spec
        candidates = [m for m in catalog
                      if m.get("backend") == backend and token in m.get("id", "")]
        if candidates:
            return max(candidates, key=lambda m: _version_key(m["id"]))["id"]

    # Rule 4 — unresolvable. Name the classes the catalog can currently satisfy.
    available = sorted(
        cls for cls, (backend, token) in _CLASS_SPEC.items()
        if any(m.get("backend") == backend and token in m.get("id", "")
               for m in catalog)
    )
    raise ValueError(
        f"Cannot resolve computer-use model {class_or_id!r}. "
        f"Pass a concrete CU-capable model id, or one of these classes "
        f"currently available: {', '.join(available) or '(none)'}."
    )


def resolve_cu_model(model: Optional[str]) -> str:
    """Resolve the CU model id for a CU cron job / CU stream (M4.1c).

    A chosen CU model is honored only when it passes the SAME capability gates
    the /models/computer-use catalog uses (CU_MODEL_FILTERS) — otherwise the CU
    streaming path could be handed an arbitrary id no driver can run. Falls back
    to CU_MODEL_DEFAULT when the model is empty/Auto ("computer-use"/"cu") OR
    when the id fails the gates.
    """
    candidate = (model or "").strip()
    if not candidate or candidate.lower() in ("computer-use", "cu"):
        return CU_MODEL_DEFAULT

    if _gate_passes(candidate):
        return candidate

    logger.warning(
        "CU model '%s' fails CU_MODEL_FILTERS; falling back to default '%s'",
        candidate, CU_MODEL_DEFAULT,
    )
    return CU_MODEL_DEFAULT
