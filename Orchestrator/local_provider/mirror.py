#!/usr/bin/env python3
"""mirror.py — Server-side model mirror catalog for the on-device Gemma provider.

The BlackBox hub mirrors the Gemma 4 LiteRT ``.litertlm`` bundles so each user's
phone downloads them FROM THE HUB over Tailscale (no per-user Hugging Face
friction). This module is ONLY the catalog: it describes WHAT is downloadable
and its metadata. The actual fetch-once + ranged download endpoint is Task 1.2 —
there is deliberately NO network and NO download/file I/O code here.

This catalog is DISTINCT from the picker catalog (``LOCAL_MODELS`` in
``local_routes``): the picker entries are id/name/provider descriptors for the
model selector; the entries here carry DOWNLOAD/METADATA fields. The slugs are
kept IDENTICAL across both so a downloaded bundle maps cleanly to a picker entry.

``size_bytes`` and ``sha256`` are ``None`` for now — they get populated by the
real fetch in Task 1.2. The catalog still lists the bundle (with None there) so
the app can render it before any download has happened.

``hf_repo`` / ``filename`` are PLACEHOLDER config: plausible LiteRT-community
ids that we will pin to the real upstream repo/file when Task 1.2 lands.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Bundle metadata — keyed by slug (identical to LOCAL_MODELS ids in
# local_routes.py). DOWNLOAD/METADATA fields only.
#
# Fields:
#   slug            stable id, matches the picker catalog entry
#   display_name    human label
#   hf_repo         Hugging Face repo id the bundle is mirrored from
#                   (PLACEHOLDER — pinned to the real repo in Task 1.2)
#   filename        the .litertlm file name within that repo
#   size_bytes      byte size of the bundle (None until the real fetch fills it)
#   sha256          content hash for verify (None until the real fetch fills it)
#   min_ram_gb      realistic minimum device RAM to run the model
#   recommended_for short human guidance on when to pick this one
# ---------------------------------------------------------------------------
BUNDLES: dict[str, dict] = {
    "gemma-4-e2b": {
        "slug": "gemma-4-e2b",
        "display_name": "Gemma 4 E2B (on-device)",
        "hf_repo": "litert-community/gemma-4-e2b-it-litert-lm",
        "filename": "gemma-4-e2b-it.litertlm",
        "size_bytes": None,  # populated by the real fetch in Task 1.2
        "sha256": None,      # populated by the real fetch in Task 1.2
        "min_ram_gb": 3.0,
        "recommended_for": "Lighter, faster on-device model for phones with less RAM.",
    },
    "gemma-4-e4b": {
        "slug": "gemma-4-e4b",
        "display_name": "Gemma 4 E4B (on-device)",
        "hf_repo": "litert-community/gemma-4-e4b-it-litert-lm",
        "filename": "gemma-4-e4b-it.litertlm",
        "size_bytes": None,  # populated by the real fetch in Task 1.2
        "sha256": None,      # populated by the real fetch in Task 1.2
        "min_ram_gb": 4.5,
        "recommended_for": "Heavier, higher-quality on-device model for high-RAM phones.",
    },
}


def list_bundles() -> list[dict]:
    """Return the bundle metadata dicts (shallow-copied).

    Each entry is a shallow ``dict()`` copy so callers can mutate the returned
    list/entries without corrupting the module-level ``BUNDLES`` (the values are
    flat, so a shallow copy is sufficient today).
    """
    return [dict(b) for b in BUNDLES.values()]


def get_bundle(slug: str) -> Optional[dict]:
    """Return a shallow copy of one bundle by slug, or ``None`` if unknown.

    Provided for Task 1.2 (the download endpoint resolves a slug → bundle); the
    copy keeps callers from mutating ``BUNDLES``.
    """
    bundle = BUNDLES.get(slug)
    return dict(bundle) if bundle is not None else None
