#!/usr/bin/env python3
"""media_relocation.py — relocate inline base64 reference images out of a task's
result_data (F2, Part 3).

MEASURED PROBLEM: 97% of Portal/tasks.db was base64 in `result_data`. Investigation
showed it is NOT generated OUTPUT (those already live on disk under Portal/uploads,
referenced by result_url / all_urls) — it is the INPUT `options.referenceImages[].data`
base64 the client uploaded for image-to-image / style-guidance generation. Those
inputs are consumed IN-MEMORY by the provider during generation and are never read
back from a completed row (no frontend reads them; nothing serves them), so they can
be relocated to disk after the fact without breaking anything.

This module holds ONE pure, dependency-light helper used by BOTH:
  * the going-forward strip in Orchestrator.tasks.process_image_generation, and
  * the one-shot migration in Orchestrator.migrations.relocate_reference_images

NON-DESTRUCTIVE by construction: the file is written to disk and its bytes are
verified BEFORE the inline base64 is dropped. On ANY error the inline `data` is
kept — the migration/producer never loses an artifact. Idempotent: an entry that
already lacks `data` (already relocated) is left untouched.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Tuple

# The subdir under UPLOADS_DIR (Portal/uploads, served at /ui/uploads) where the
# relocated reference-image inputs land. Reusing the EXISTING served path — no new
# storage scheme — so the relocated files resolve at /ui/uploads/refimages/<file>.
REFIMAGES_SUBDIR = "refimages"

# mimeType -> file extension. Falls back to .bin for anything unrecognized (still
# a lossless byte copy; only the filename suffix is generic).
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _ext_for_mime(mime: Any) -> str:
    return _MIME_EXT.get(str(mime or "").strip().lower(), ".bin")


def relocate_reference_images(
    result_data: Any, task_id: str, uploads_dir: Any
) -> Tuple[Any, int]:
    """Relocate inline base64 `options.referenceImages[].data` to files on disk.

    Each `{"data": "<base64>", "mimeType": "..."}` entry becomes
    `{"url": "/ui/uploads/refimages/<task_id>_ref<i>.<ext>", "mimeType": "...",
      "relocated": true}` — the giant base64 leaves the DB, a compact served
    reference stays. All other keys on the entry are preserved.

    Args:
        result_data: the task's result_data dict (or anything — non-dicts pass through).
        task_id:     used to name the relocated files (unguessable UUID in practice).
        uploads_dir: the UPLOADS_DIR (Portal/uploads). Files go under <uploads_dir>/refimages.

    Returns:
        (new_result_data, relocated_count). Returns the SAME object unchanged (and
        count 0) when there is nothing to relocate. Never mutates the caller's dict —
        a shallow copy of the touched levels is returned when a change is made.

    Guarantees:
        * NON-DESTRUCTIVE: the file is written and its byte length re-read/verified
          BEFORE `data` is dropped. Any decode/write/verify failure keeps the inline
          `data` for that entry (the artifact is never lost).
        * IDEMPOTENT: an entry without a `data` key (already relocated) is left as-is,
          so re-running the migration is a no-op on already-migrated rows.
    """
    if not isinstance(result_data, dict):
        return result_data, 0
    opts = result_data.get("options")
    if not isinstance(opts, dict):
        return result_data, 0
    refs = opts.get("referenceImages")
    if not isinstance(refs, list) or not refs:
        return result_data, 0

    dest_dir = Path(uploads_dir) / REFIMAGES_SUBDIR
    relocated = 0
    new_refs = []

    for i, ref in enumerate(refs):
        if not isinstance(ref, dict) or not ref.get("data"):
            # Already relocated / malformed / no inline bytes — leave untouched.
            new_refs.append(ref)
            continue

        b64 = ref.get("data")
        mime = ref.get("mimeType") or ref.get("mime_type")
        try:
            raw = base64.b64decode(b64, validate=False)
            if not raw:
                raise ValueError("empty decode")
            dest_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{task_id}_ref{i}{_ext_for_mime(mime)}"
            fpath = dest_dir / fname
            fpath.write_bytes(raw)
            # Verify the artifact landed intact BEFORE dropping the inline base64.
            if fpath.stat().st_size != len(raw):
                raise IOError(
                    f"size mismatch after write: {fpath.stat().st_size} != {len(raw)}"
                )
            new_ref = {k: v for k, v in ref.items() if k != "data"}
            new_ref["url"] = f"/ui/uploads/{REFIMAGES_SUBDIR}/{fname}"
            new_ref["relocated"] = True
            new_refs.append(new_ref)
            relocated += 1
        except Exception as e:  # noqa: BLE001 — keep the inline data on ANY failure
            print(
                f"[MEDIA-RELOCATE] task {task_id} ref[{i}] relocate failed "
                f"(kept inline, no data loss): {e}"
            )
            new_refs.append(ref)

    if relocated == 0:
        return result_data, 0

    # Copy only the touched levels — never mutate the caller's dict in place.
    new_opts = dict(opts)
    new_opts["referenceImages"] = new_refs
    new_rd = dict(result_data)
    new_rd["options"] = new_opts
    return new_rd, relocated
