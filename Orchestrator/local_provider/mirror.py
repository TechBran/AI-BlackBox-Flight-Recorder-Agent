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

``hf_repo`` / ``filename`` are now PINNED to the REAL upstream litert-community
repos (Task 2.6a): the ungated, Apache-2.0 ``litert-community/gemma-4-E*B-it-litert-lm``
bundles (no HF token needed). ``size_bytes`` is the approximate bundle size;
``sha256`` is still ``None`` (verification is skipped when the digest is unknown —
acceptable for v1) and will be pinned to the real content hash in Task 2.6b once
verified on-device.
"""

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# On-disk mirror cache dir. MODULE attribute (not created at import) so tests
# can monkeypatch it (same pattern as the registry's STORE_FILE). The dir is
# created lazily by ensure_present on the first real download.
# ---------------------------------------------------------------------------
MIRROR_DIR: Path = Path(__file__).parent / "mirror_store"

# Streamed read chunk size for hashing (1 MiB).
_HASH_CHUNK = 1024 * 1024

# ---------------------------------------------------------------------------
# Bundle metadata — keyed by slug (identical to LOCAL_MODELS ids in
# local_routes.py). DOWNLOAD/METADATA fields only.
#
# Fields:
#   slug            stable id, matches the picker catalog entry
#   display_name    human label
#   hf_repo         Hugging Face repo id the bundle is mirrored from
#                   (PINNED to the real litert-community repo — Task 2.6a)
#   filename        the .litertlm file name within that repo
#   size_bytes      approximate byte size of the bundle
#   sha256          content hash for verify (None — skip verify until pinned in 2.6b)
#   min_ram_gb      realistic minimum device RAM to run the model
#   recommended_for short human guidance on when to pick this one
#
# Per-model runtime config (Task W6) — snake_case to match the <slug>.json
# sidecar / API JSON the phone consumes (LocalBundle.toModelConfig):
#   recommended     this is the recommended default model (E4B; the picker
#                   surfaces it + its note). E2B is labeled experimental.
#   context_note    human-readable note shown next to the model in the picker
#   max_tokens      context window (input+output) for the engine's EngineConfig
#   support_image   whether the bundle accepts image input (both E2B/E4B are
#                   multimodal; W4 wires the vision backend, this only CARRIES it)
# ---------------------------------------------------------------------------
BUNDLES: dict[str, dict] = {
    "gemma-4-e2b": {
        "slug": "gemma-4-e2b",
        "display_name": "Gemma 4 E2B (on-device)",
        "hf_repo": "litert-community/gemma-4-E2B-it-litert-lm",
        "filename": "gemma-4-E2B-it.litertlm",
        "size_bytes": 2590000000,  # ~2.59 GB (approx)
        "sha256": None,  # TODO(2.6b): pin real sha256 (verify skipped while None)
        "min_ram_gb": 3.0,
        "recommended_for": "Lighter, faster on-device model for phones with less RAM.",
        # Per-model config (Task W6). E2B is the EXPERIMENTAL fallback: it runs
        # on lower-RAM phones but is weaker at multi-step agent (tool) loops, so
        # it is NOT the recommended default — E4B is.
        "recommended": False,
        "context_note": "Experimental — weaker at multi-step agent loops",
        "max_tokens": 16384,
        "support_image": True,
    },
    "gemma-4-e4b": {
        "slug": "gemma-4-e4b",
        "display_name": "Gemma 4 E4B (on-device)",
        "hf_repo": "litert-community/gemma-4-E4B-it-litert-lm",
        "filename": "gemma-4-E4B-it.litertlm",
        "size_bytes": 3660000000,  # ~3.66 GB (approx)
        "sha256": None,  # TODO(2.6b): pin real sha256 (verify skipped while None)
        "min_ram_gb": 4.5,
        "recommended_for": "Heavier, higher-quality on-device model for high-RAM phones.",
        # Per-model config (Task W6). E4B is the RECOMMENDED default: best
        # on-device agent (multi-step tool-loop) reliability. Multimodal.
        "recommended": True,
        "context_note": "Recommended — best on-device agent reliability",
        "max_tokens": 16384,
        "support_image": True,
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


# ---------------------------------------------------------------------------
# Task 1.2 — fetch-once + ranged download support
# ---------------------------------------------------------------------------

def _download_bundle(bundle: dict, dest_path: Path) -> None:
    """Fetch ONE bundle's bytes from Hugging Face into ``dest_path``.

    This is the ONLY function in this module that touches the network — it is a
    separate module-level function precisely so tests monkeypatch IT and stay
    hermetic (no real HF request ever runs under pytest).

    Prefers ``huggingface_hub.hf_hub_download`` if the package is importable
    (then copies the cached file to ``dest_path``); otherwise falls back to a
    streamed ``requests`` GET against the HF resolve URL. Either way the token
    comes from ``$HF_TOKEN`` (optional — public repos need none).
    """
    repo = bundle["hf_repo"]
    filename = bundle["filename"]
    token = os.environ.get("HF_TOKEN")

    try:
        from huggingface_hub import hf_hub_download  # type: ignore
    except Exception:
        hf_hub_download = None

    if hf_hub_download is not None:
        cached = hf_hub_download(repo_id=repo, filename=filename, token=token)
        # Copy out of the HF cache so our MIRROR_DIR owns a stable, standalone file.
        shutil.copyfile(cached, dest_path)
        return

    # Fallback: streamed requests GET against the resolve URL.
    import requests

    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=_HASH_CHUNK):
                if chunk:
                    f.write(chunk)


def ensure_present(slug: str) -> Path:
    """Return the local path to a bundle's bytes, fetching them ONCE if needed.

    Fetch-once semantics: if the target file already exists (and is non-empty)
    it is returned as-is with NO re-download; otherwise it is downloaded via
    ``_download_bundle`` into ``MIRROR_DIR`` (created lazily here) and the path
    returned.

    Atomic: the fetch writes to a ``<filename>.part`` temp path and is only
    promoted to the final target via ``os.replace`` (atomic rename) on success,
    so a mid-download failure can never cache a truncated bundle. On ANY failure
    the ``.part`` is removed and the error re-raised; the final target is never
    left in a half-written state.

    Raises ``KeyError`` for an unknown slug.
    """
    bundle = get_bundle(slug)
    if bundle is None:
        raise KeyError(f"unknown bundle: {slug}")

    target = MIRROR_DIR / bundle["filename"]
    if target.exists() and target.stat().st_size > 0:
        return target

    MIRROR_DIR.mkdir(parents=True, exist_ok=True)

    # Download into a temp path, then atomically rename — a failed/partial fetch
    # can never poison the cache (covers BOTH the hf_hub and requests branches of
    # _download_bundle since atomicity lives out here, not inside the fetch).
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        _download_bundle(bundle, tmp)
        os.replace(tmp, target)  # atomic promotion
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Optional integrity check: only when the catalog carries a sha256 (None
    # today). Once catalog hashes are populated this verifies the served bundle.
    # TODO: presence == size>0 until catalog hashes are populated; once set, this
    # verifies integrity.
    expected_sha = bundle.get("sha256")
    if expected_sha:
        actual_sha = bundle_sha256(target)
        if actual_sha != expected_sha:
            target.unlink(missing_ok=True)
            raise ValueError(
                f"sha256 mismatch for {slug}: expected {expected_sha}, got {actual_sha}"
            )

    return target


def bundle_sha256(path: Path) -> str:
    """Return the streamed SHA-256 hex digest of the file at ``path``.

    Streams the file in chunks so hashing a multi-GB bundle does not load it all
    into memory. Used so a served bundle's integrity can be reported.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
