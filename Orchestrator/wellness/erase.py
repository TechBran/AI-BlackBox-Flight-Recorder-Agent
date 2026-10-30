"""Stop & erase, and the operator-delete hook (spec §3.6).

erase_operator() revokes the device key (and a pending one), tells the phone
to forget (best effort), scrubs the tasks.db rows the store listed in
task_ref, unlinks the operator's DB with its -wal/-shm, and removes the
enrollment. What it never touches: the ledger (health turns are minted with
their numbers masked, so the UI says "Your past health conversations stay in
your BlackBox memory, but they contain no health numbers").

Called by POST /wellness/erase and /wellness/disconnect (keep_history=false),
both signed by the enrolled phone, and by DELETE /operator/{name}. Counts only
in the log.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from Orchestrator.wellness import enroll, paths, store

logger = logging.getLogger(__name__)


def _scrub_tasks(task_ids: List[str], operator: str) -> int:
    """Delete the operator's task rows a wellness turn left in tasks.db."""
    if not task_ids:
        return 0
    try:
        from Orchestrator import models
        conn = models.task_db._connect()
    except Exception as e:  # noqa: BLE001 - erase goes on without it
        logger.warning("[WELLNESS] erase: tasks.db not reachable (%s)", type(e).__name__)
        return 0
    try:
        n = 0
        for i in range(0, len(task_ids), 200):
            chunk = task_ids[i:i + 200]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(f"DELETE FROM tasks WHERE operator=? AND task_id IN ({marks})",
                               [operator, *chunk])
            n += cur.rowcount or 0
        conn.commit()
        return n
    finally:
        conn.close()


def erase_operator(operator: str, *, reason: str = "erase", unbind: bool = True,
                   now_ms: Optional[int] = None) -> dict:
    """Erase everything BlackBox stored for this operator's health. Idempotent."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rec = enroll.get_enrollment(operator)
    if not rec:
        return {"erased": False, "files": 0, "tasks": 0}
    unbound = None
    if unbind and rec.get("state") in enroll.STORED_LIVE:
        unbound = enroll.unbind_phone(rec)
    enroll.revoke_key(rec.get("enrollment_id"), "revoked", now_ms)
    if rec.get("pending"):
        enroll.revoke_key(rec["pending"].get("enrollment_id"), "revoked", now_ms)
    tasks = files = 0
    op_key = rec.get("op_key")
    if paths.valid_op_key(op_key):
        st = enroll.store_for(rec, create=False)
        if st is not None:
            try:
                tasks = _scrub_tasks(st.task_refs(), operator)
            except Exception as e:  # noqa: BLE001
                logger.warning("[WELLNESS] erase: task scrub failed (%s)", type(e).__name__)
        files = len(store.delete_store_files(op_key))
    with enroll.editing() as data:
        data.pop(operator, None)
    logger.info("[WELLNESS] erase (%s): files=%d tasks=%d unbind=%s", reason, files, tasks,
                unbound)
    return {"erased": True, "files": files, "tasks": tasks}


def on_operator_deleted(operator: str) -> None:
    """DELETE /operator/{name}: the hook. Never raises into the route."""
    try:
        erase_operator(operator, reason="operator_deleted")
    except Exception as e:  # noqa: BLE001
        logger.error("[WELLNESS] operator-delete erase failed (%s)", type(e).__name__)
