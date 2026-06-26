"""
SQLite-based SMS message store for the AI BlackBox Flight Recorder.

Stores inbound/outbound SMS messages per operator with conversation
threading, unread tracking, and thread listing.
"""

import sqlite3
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sms.store")

DB_PATH = Path(__file__).resolve().parent.parent.parent / "Manifest" / "sms_messages.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,
    direction TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    contact_name TEXT DEFAULT '',
    body TEXT NOT NULL,
    ai_response TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    status TEXT DEFAULT 'delivered',
    read INTEGER DEFAULT 0,
    line_number TEXT DEFAULT '',
    gateway_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_operator ON messages(operator);
CREATE INDEX IF NOT EXISTS idx_phone ON messages(phone_number);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);
"""

# Additive columns that may be missing on DBs created before per-line scoping.
# (column_name, column_def) — applied via idempotent ALTER TABLE migration.
_ADDITIVE_COLUMNS = (
    ("line_number", "TEXT DEFAULT ''"),
    ("gateway_id", "TEXT DEFAULT ''"),
)


def _normalize_phone(phone: str) -> str:
    """Strip a phone number down to last 10 digits for matching."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


class MessageStore:
    """Thread-safe SQLite message store for SMS conversations."""

    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        log.info("MessageStore initialized at %s", DB_PATH)

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Idempotent migration for DBs created before additive columns.
            existing = {
                r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            for name, col_def in _ADDITIVE_COLUMNS:
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {col_def}"
                    )
                    log.info("Migrated messages table: added column %s", name)
            self._conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return dict(row)

    def _rows_to_dicts(self, rows: list) -> list:
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def store_message(
        self,
        operator: str,
        direction: str,
        phone_number: str,
        contact_name: str,
        body: str,
        ai_response: str = "",
        timestamp: str | None = None,
        status: str = "delivered",
        line_number: str = "",
        gateway_id: str = "",
    ) -> int:
        """Store a message and return its row ID."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        normalized = _normalize_phone(phone_number)
        normalized_line = _normalize_phone(line_number) if line_number else ""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO messages
                       (operator, direction, phone_number, contact_name,
                        body, ai_response, timestamp, status, read,
                        line_number, gateway_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (operator, direction, normalized, contact_name,
                     body, ai_response, timestamp, status,
                     normalized_line, gateway_id),
                )
                self._conn.commit()
                msg_id = cur.lastrowid
                log.debug("Stored %s message id=%d for %s", direction, msg_id, normalized)
                return msg_id
            except Exception:
                log.exception("Failed to store message")
                raise

    def get_messages(self, operator: str, limit: int = 50, offset: int = 0) -> list:
        """Get all messages for an operator, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM messages
                   WHERE operator = ?
                   ORDER BY timestamp DESC
                   LIMIT ? OFFSET ?""",
                (operator, limit, offset),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def get_conversation(
        self, operator: str, phone_number: str, limit: int = 50, offset: int = 0,
        line_number: str | None = None, recent: bool = False,
    ) -> list:
        """Get messages between operator and a phone number, oldest first (chat order).

        When ``line_number`` is None, return messages across all of our lines
        (back-compatible). When provided, scope to that one line.

        Ordering uses ``(timestamp, id)`` so a same-second inbound/outbound pair
        sorts deterministically by insertion id (inbound before its outbound
        reply), not by SQLite's incidental row order.

        ``recent``:
          - ``False`` (default, back-compatible): return the OLDEST ``limit``
            messages from ``offset`` (``ORDER BY timestamp, id ASC`` then
            ``LIMIT/OFFSET``).
          - ``True``: return the most RECENT ``limit`` messages, presented
            oldest-first (chronological). The window is the last N — fetch
            DESC + LIMIT then reverse — so a long thread yields the tail, not
            the head. ``offset`` is ignored in this mode.
        """
        normalized = _normalize_phone(phone_number)
        where = "operator = ? AND phone_number = ?"
        params: list = [operator, normalized]
        if line_number is not None:
            where += " AND line_number = ?"
            params.append(_normalize_phone(line_number) if line_number else "")
        if recent:
            # Newest-N window: take the last ``limit`` rows (DESC), then reverse
            # in Python so the caller still receives them oldest-first.
            params.append(limit)
            with self._lock:
                rows = self._conn.execute(
                    f"""SELECT * FROM messages
                        WHERE {where}
                        ORDER BY timestamp DESC, id DESC
                        LIMIT ?""",
                    params,
                ).fetchall()
            rows = list(reversed(rows))
            return self._rows_to_dicts(rows)
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM messages
                    WHERE {where}
                    ORDER BY timestamp, id
                    LIMIT ? OFFSET ?""",
                params,
            ).fetchall()
        return self._rows_to_dicts(rows)

    def get_unread_count(self, operator: str) -> int:
        """Count unread inbound messages for an operator."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS cnt FROM messages
                   WHERE operator = ? AND direction = 'inbound' AND read = 0""",
                (operator,),
            ).fetchone()
        return row["cnt"] if row else 0

    def mark_read(self, message_id: int) -> None:
        """Mark a single message as read."""
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE messages SET read = 1 WHERE id = ?",
                    (message_id,),
                )
                self._conn.commit()
            except Exception:
                log.exception("Failed to mark message %d as read", message_id)
                raise

    def mark_all_read(self, operator: str, phone_number: str) -> None:
        """Mark every message in a conversation as read."""
        normalized = _normalize_phone(phone_number)
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE messages SET read = 1
                       WHERE operator = ? AND phone_number = ?""",
                    (operator, normalized),
                )
                self._conn.commit()
                log.debug("Marked all read: operator=%s phone=%s", operator, normalized)
            except Exception:
                log.exception("Failed to mark conversation read")
                raise

    def get_recent_threads(self, operator: str, line_number: str | None = None) -> list:
        """Return unique phone-number threads with last message preview and unread count.

        When ``line_number`` is None, threads span all of our lines
        (back-compatible). When provided, scope the thread list to that one line.

        Returns list of dicts sorted by last_timestamp descending:
            {phone_number, contact_name, last_message, last_timestamp,
             unread_count, direction, line_number}
        """
        normalized_line = (
            (_normalize_phone(line_number) if line_number else "")
            if line_number is not None
            else None
        )
        # Optional per-line filter applied to both inner aggregates.
        line_clause = " AND line_number = ?" if normalized_line is not None else ""
        # Each inner query takes (operator[, line]); the JOIN uses operator again.
        inner = [operator] + ([normalized_line] if normalized_line is not None else [])
        params = inner + [operator] + inner

        sql = f"""
            SELECT
                m.phone_number,
                m.contact_name,
                m.body        AS last_message,
                m.timestamp   AS last_timestamp,
                m.direction,
                m.line_number,
                COALESCE(u.unread_count, 0) AS unread_count
            FROM messages m
            INNER JOIN (
                SELECT phone_number, MAX(timestamp) AS max_ts
                FROM messages
                WHERE operator = ?{line_clause}
                GROUP BY phone_number
            ) latest
                ON m.phone_number = latest.phone_number
               AND m.timestamp   = latest.max_ts
               AND m.operator    = ?
            LEFT JOIN (
                SELECT phone_number, COUNT(*) AS unread_count
                FROM messages
                WHERE operator = ? AND direction = 'inbound' AND read = 0{line_clause}
                GROUP BY phone_number
            ) u ON m.phone_number = u.phone_number
            ORDER BY last_timestamp DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)
