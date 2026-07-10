#!/usr/bin/env python3
"""One-shot migration: relocate inline base64 reference images out of tasks.db
result_data onto disk, and backfill the device_id column (F2, Parts 1-3).

WHAT & WHY (measured): Portal/tasks.db was 263 MB, of which ~250 MB (97%) was
`options.referenceImages[].data` base64 in 26 image_generation rows. See
Orchestrator/media_relocation for the full investigation — these are INPUT
reference images (already consumed by the provider at generation time, never read
back), so relocating them to disk is safe and lossless.

This script, run ONCE against the live DB (BACK IT UP FIRST — `cp Portal/tasks.db
Portal/tasks.db.bak`), performs three idempotent steps:

  1. CREATE INDEX IF NOT EXISTS idx_tasks_created_at  (Part 1 — kills the TEMP
     B-TREE sort on /tasks/list; takes effect for the live process immediately
     since TaskDatabase opens a fresh connection per query).
  2. Backfill the device_id column from result_data for existing rows (Part 2 —
     so the projected /tasks/list returns device_id WITHOUT touching result_data).
  3. Relocate every inline referenceImages[].data blob to a file and rewrite the
     row's result_data with a compact URL reference (Part 3), then VACUUM to
     reclaim the freed pages.

Run:  python -m Orchestrator.migrations.relocate_reference_images [db_path] [uploads_dir]
Defaults: db_path=Portal/tasks.db  uploads_dir=Portal/uploads
Add --dry-run to report what WOULD change without writing.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Tuple

from Orchestrator.media_relocation import relocate_reference_images


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently bring the schema up to F2 so this migration is self-sufficient
    on a pre-F2 live DB whose running process has NOT restarted yet:
      * ADD COLUMN device_id (Part 2) — guarded like models._init_db's ALTERs, so
        backfill_device_id below has a column to write, and
      * CREATE INDEX idx_tasks_created_at (Part 1) — kills the TEMP B-TREE sort.
    """
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN device_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)"
    )


def backfill_device_id(conn: sqlite3.Connection) -> int:
    """Part 2 — copy result_data->>'$.device_id' into the device_id column for rows
    that have it in the JSON but a NULL column. Idempotent (WHERE device_id IS NULL)."""
    cur = conn.execute(
        """
        UPDATE tasks
           SET device_id = json_extract(result_data, '$.device_id')
         WHERE device_id IS NULL
           AND result_data IS NOT NULL
           AND json_valid(result_data)
           AND json_extract(result_data, '$.device_id') IS NOT NULL
        """
    )
    return cur.rowcount


def _count_would_backfill(conn: sqlite3.Connection) -> int:
    """Dry-run count for device_id backfill — read-only, works even before the
    device_id column exists (does not reference it)."""
    return conn.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE result_data IS NOT NULL AND json_valid(result_data) "
        "AND json_extract(result_data, '$.device_id') IS NOT NULL"
    ).fetchone()[0]


def relocate_all(
    conn: sqlite3.Connection, uploads_dir: str, dry_run: bool = False
) -> Tuple[int, int]:
    """Part 3 — relocate inline referenceImages base64 to disk for every row that
    still has it. Returns (rows_changed, images_relocated). Idempotent: already
    relocated rows (no inline `data`) yield 0 and are skipped.

    dry_run is FULLY side-effect-free: it counts inline blobs by inspecting the JSON
    and does NOT call relocate_reference_images (which writes files) or UPDATE."""
    cur = conn.execute(
        "SELECT task_id, result_data FROM tasks "
        "WHERE task_type = 'image_generation' "
        "AND result_data IS NOT NULL "
        "AND result_data LIKE '%referenceImages%'"
    )
    targets = cur.fetchall()
    rows_changed = 0
    images_relocated = 0
    for task_id, rd_json in targets:
        try:
            rd = json.loads(rd_json)
        except (TypeError, ValueError):
            continue
        if dry_run:
            refs = (rd.get("options") or {}).get("referenceImages") or []
            n = sum(1 for r in refs if isinstance(r, dict) and r.get("data"))
            if n:
                rows_changed += 1
                images_relocated += n
            continue
        new_rd, n = relocate_reference_images(rd, task_id, uploads_dir)
        if n:
            images_relocated += n
            rows_changed += 1
            conn.execute(
                "UPDATE tasks SET result_data = ? WHERE task_id = ?",
                (json.dumps(new_rd), task_id),
            )
    return rows_changed, images_relocated


def run(db_path: str = "Portal/tasks.db",
        uploads_dir: str = "Portal/uploads",
        dry_run: bool = False) -> dict:
    """Execute all three steps against db_path. Returns a summary dict."""
    before_size = Path(db_path).stat().st_size if Path(db_path).exists() else 0

    # Generous busy_timeout so a VACUUM/UPDATE waits for a write window rather than
    # failing on a live WAL DB the service is occasionally writing to.
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        cur = conn.execute("SELECT COUNT(*) FROM tasks")
        row_count_before = cur.fetchone()[0]

        if dry_run:
            # FULLY read-only: report what WOULD change, alter nothing.
            device_backfilled = _count_would_backfill(conn)
            rows_changed, images_relocated = relocate_all(conn, uploads_dir, True)
            conn.rollback()
        else:
            ensure_schema(conn)
            device_backfilled = backfill_device_id(conn)
            rows_changed, images_relocated = relocate_all(conn, uploads_dir, False)
            conn.commit()
            # VACUUM must run OUTSIDE a transaction. sqlite3 does not auto-open one
            # for VACUUM, and we just committed, so this is safe.
            conn.execute("VACUUM")
            conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM tasks")
        row_count_after = cur.fetchone()[0]
    finally:
        conn.close()

    after_size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
    summary = {
        "dry_run": dry_run,
        "row_count_before": row_count_before,
        "row_count_after": row_count_after,
        "device_id_backfilled": device_backfilled,
        "rows_relocated": rows_changed,
        "images_relocated": images_relocated,
        "db_bytes_before": before_size,
        "db_bytes_after": after_size,
    }
    return summary


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if not a.startswith("--")]
    db_path = argv[0] if len(argv) > 0 else "Portal/tasks.db"
    uploads_dir = argv[1] if len(argv) > 1 else "Portal/uploads"

    print(f"[MIGRATE] db={db_path} uploads={uploads_dir} dry_run={dry_run}")
    summary = run(db_path, uploads_dir, dry_run)
    for k, v in summary.items():
        print(f"[MIGRATE]   {k}: {v}")
    if summary["row_count_before"] != summary["row_count_after"]:
        print("[MIGRATE] ERROR: row count changed! Aborting-worthy — investigate.")
        return 1
    print("[MIGRATE] done — row count preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
