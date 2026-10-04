"""SQLite persistence layer.

Design
------
* SQLite is the authoritative state. In-memory structures are only ever an
  optimisation, never a requirement for recovery.
* Migrations are idempotent, transactional and additive: an existing legacy
  ``media_items`` table keeps all of its rows and simply gains columns.
* Every state change is a compare-and-set (``UPDATE ... WHERE id=? AND status
  IN (...)``) so concurrent handlers can never double-publish.
* Foreign keys are enforced on every connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

#: Media types this bot is allowed to publish.
SUPPORTED_MEDIA_TYPES: Tuple[str, ...] = ("video", "animation", "audio", "photo", "document")


class Status:
    """Canonical record states."""

    COLLECTING = "collecting"
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELED = "canceled"


ALL_STATES: Tuple[str, ...] = (
    Status.COLLECTING,
    Status.PENDING,
    Status.PUBLISHING,
    Status.PUBLISHED,
    Status.FAILED,
    Status.UNCERTAIN,
    Status.CANCELED,
)

#: States from which a Telegram send may be attempted.
PUBLISHABLE_STATES: Tuple[str, ...] = (Status.PENDING, Status.FAILED)

#: States an admin may still cancel from.
CANCELABLE_STATES: Tuple[str, ...] = (Status.PENDING, Status.FAILED)

#: States an admin may still edit the episode number in.
EDITABLE_STATES: Tuple[str, ...] = (Status.PENDING, Status.FAILED)

ITEMS_TABLE = "media_items"
ALBUMS_TABLE = "albums"
ALBUM_ITEMS_TABLE = "album_items"

#: Columns added in schema v2 for ``media_items`` (applied to legacy databases).
_MEDIA_ITEMS_V2_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("uncertain", "INTEGER NOT NULL DEFAULT 0"),
    ("publishing_started_at", "TEXT"),
    ("preview_message_id", "INTEGER"),
    ("last_error", "TEXT"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
)


def now_iso() -> str:
    """Current UTC time as a fixed-width ISO-8601 string.

    Fixed width matters: these values are compared lexicographically in SQL.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Database:
    """Async SQLite wrapper with explicit lifecycle and CAS state transitions."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._db_path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self.schema_version: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Optional[str]:
        return self._db_path

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self, path: Optional[str] = None) -> "Database":
        """Open the database, apply pragmas and run migrations."""
        if self._conn is not None:
            return self
        target = path or self._db_path
        if not target:
            raise RuntimeError("Database path is not configured.")
        self._db_path = target

        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)

        conn = await aiosqlite.connect(target)
        # Manual transaction control (see _transaction).
        conn.isolation_level = None
        conn.row_factory = aiosqlite.Row
        self._conn = conn

        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA busy_timeout = 10000")
        await conn.execute("PRAGMA synchronous = NORMAL")

        await self._migrate()
        logger.info("database-ready path=%s schema_version=%d", self._safe_path(), self.schema_version)
        return self

    def _safe_path(self) -> str:
        """Database file name only - never the full local filesystem path."""
        return os.path.basename(self._db_path) if self._db_path else "<unset>"

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("database-closed")

    async def __aenter__(self) -> "Database":
        return await self.connect()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Migrations
    # ------------------------------------------------------------------ #

    async def _columns(self, table: str) -> List[str]:
        assert self._conn is not None
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["name"] for row in rows]

    async def _table_exists(self, table: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    @asynccontextmanager
    async def _transaction(self):
        """Explicit IMMEDIATE transaction with rollback on failure.

        The lock is held for the whole block so two coroutines can never
        interleave BEGIN/COMMIT on the shared connection. Callers must not use
        the ``_fetch_*``/``_cas`` helpers inside the block (they take the same
        non-reentrant lock).
        """
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                try:
                    await self._conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - rollback best effort
                    logger.exception("rollback-failed")
                raise
            else:
                await self._conn.execute("COMMIT")

    async def _migrate(self) -> None:
        assert self._conn is not None
        logger.info("migrate-start target_version=%d", SCHEMA_VERSION)

        async with self._transaction() as conn:
            existing = await self._table_exists(ITEMS_TABLE)
            await conn.execute(_CREATE_MEDIA_ITEMS)
            if existing:
                # Legacy database: keep all rows, only add the new columns.
                present = set(await self._columns(ITEMS_TABLE))
                for column, decl in _MEDIA_ITEMS_V2_COLUMNS:
                    if column not in present:
                        await conn.execute(
                            f"ALTER TABLE {ITEMS_TABLE} ADD COLUMN {column} {decl}"
                        )
                        logger.info("migration-added-column table=%s column=%s", ITEMS_TABLE, column)
            await conn.execute(_CREATE_ALBUMS)
            await conn.execute(_CREATE_ALBUM_ITEMS)
            for statement in _INDEXES:
                await conn.execute(statement)
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        cursor = await self._conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        self.schema_version = int(row[0]) if row else 0
        logger.info("migrate-complete schema_version=%d", self.schema_version)

    # ------------------------------------------------------------------ #
    # Generic helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _placeholders(values: Sequence[Any]) -> str:
        return ",".join("?" for _ in values)

    async def _cas(
        self,
        table: str,
        row_id: int,
        from_states: Sequence[str],
        to_state: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Atomically move a row between states.

        Returns ``True`` only when exactly one row changed, which is what makes
        double-publishing impossible even under concurrent callbacks.
        """
        assert self._conn is not None
        sets = ["status = ?", "updated_at = ?", "version = version + 1"]
        values: List[Any] = [to_state, now_iso()]
        for column, value in (extra or {}).items():
            sets.append(f"{column} = ?")
            values.append(value)
        values.append(row_id)
        values.extend(from_states)
        sql = (
            f"UPDATE {table} SET {', '.join(sets)} "
            f"WHERE id = ? AND status IN ({self._placeholders(from_states)})"
        )
        async with self._lock:
            cursor = await self._conn.execute(sql, values)
            changed = cursor.rowcount
            return changed == 1

    async def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
        return dict(row) if row is not None else None

    async def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Single media items
    # ------------------------------------------------------------------ #

    async def insert_media_item(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
        topic_id: int,
        sender_id: int,
        media_type: str,
        file_id: str,
        file_unique_id: str = "",
        caption: Optional[str] = None,
        anime_title: str = "",
        emoji: str = "",
        episode_number: Optional[str] = None,
        media_group_id: Optional[str] = None,
        status: str = Status.PENDING,
        final_caption: str = "",
    ) -> Tuple[bool, int]:
        """Insert a single-media record.

        Returns ``(inserted, row_id)``. ``inserted`` is ``False`` when the
        source message was already known - uniqueness is enforced by the
        database, not by a pre-check, so concurrent arrivals are safe.
        """
        assert self._conn is not None
        timestamp = now_iso()
        try:
            async with self._transaction() as conn:
                cursor = await conn.execute(
                    f"""INSERT INTO {ITEMS_TABLE} (
                            source_chat_id, source_message_id, media_group_id, topic_id, sender_id,
                            media_type, file_id, file_unique_id, caption, anime_title, emoji,
                            episode_number, status, final_caption, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_chat_id, source_message_id, media_group_id, topic_id, sender_id,
                        media_type, file_id, file_unique_id or "", caption, anime_title, emoji,
                        episode_number, status, final_caption, timestamp, timestamp,
                    ),
                )
                return True, int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            logger.info(
                "duplicate-item-rejected chat=%s message=%s",
                source_chat_id, source_message_id,
            )
            return False, 0

    async def get_item(self, row_id: int) -> Optional[Dict[str, Any]]:
        return await self._fetch_one(f"SELECT * FROM {ITEMS_TABLE} WHERE id = ?", (row_id,))

    async def get_item_by_source(self, source_chat_id: int, source_message_id: int) -> Optional[Dict[str, Any]]:
        return await self._fetch_one(
            f"SELECT * FROM {ITEMS_TABLE} WHERE source_chat_id = ? AND source_message_id = ?",
            (source_chat_id, source_message_id),
        )

    async def claim_item_for_publishing(self, row_id: int) -> bool:
        """pending|failed -> publishing (atomic gate before any Telegram send)."""
        return await self._cas(
            ITEMS_TABLE, row_id, PUBLISHABLE_STATES, Status.PUBLISHING,
            {"publishing_started_at": now_iso(), "last_error": None},
        )

    async def finish_item_published(
        self,
        row_id: int,
        *,
        target_chat_id: str,
        target_message_ids: Sequence[int],
        final_caption: str,
    ) -> bool:
        return await self._cas(
            ITEMS_TABLE, row_id, (Status.PUBLISHING,), Status.PUBLISHED,
            {
                "target_chat_id": str(target_chat_id),
                "target_message_id": ",".join(str(m) for m in target_message_ids),
                "published_at": now_iso(),
                "final_caption": final_caption,
                "uncertain": 0,
                "last_error": None,
            },
        )

    async def finish_item_failed(self, row_id: int, error: str) -> bool:
        """publishing -> failed. Only for a *clear* Telegram rejection."""
        return await self._cas(
            ITEMS_TABLE, row_id, (Status.PUBLISHING,), Status.FAILED,
            {"last_error": error[:500], "uncertain": 0},
        )

    async def mark_item_uncertain(self, row_id: int, reason: str) -> bool:
        """publishing -> uncertain. The outcome is unknown and must not be retried."""
        return await self._cas(
            ITEMS_TABLE, row_id, (Status.PUBLISHING,), Status.UNCERTAIN,
            {"last_error": reason[:500], "uncertain": 1},
        )

    async def cancel_item(self, row_id: int) -> bool:
        return await self._cas(
            ITEMS_TABLE, row_id, CANCELABLE_STATES, Status.CANCELED, {"last_error": None},
        )

    async def resolve_item_uncertain_published(
        self, row_id: int, target_message_ids: Optional[Sequence[int]] = None
    ) -> bool:
        extra: Dict[str, Any] = {
            "uncertain": 0,
            "published_at": now_iso(),
            "last_error": "resolved by admin: confirmed published",
        }
        if target_message_ids:
            extra["target_message_id"] = ",".join(str(m) for m in target_message_ids)
        return await self._cas(ITEMS_TABLE, row_id, (Status.UNCERTAIN,), Status.PUBLISHED, extra)

    async def resolve_item_uncertain_absent(self, row_id: int) -> bool:
        return await self._cas(
            ITEMS_TABLE, row_id, (Status.UNCERTAIN,), Status.FAILED,
            {"uncertain": 0, "last_error": "resolved by admin: confirmed not published"},
        )

    async def set_item_episode(self, row_id: int, episode_number: str, final_caption: str) -> bool:
        """Update the episode number, only from an editable state."""
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(
                f"UPDATE {ITEMS_TABLE} SET episode_number = ?, final_caption = ?, updated_at = ?, "
                f"version = version + 1 WHERE id = ? AND status IN ({self._placeholders(EDITABLE_STATES)})",
                (episode_number, final_caption, now_iso(), row_id, *EDITABLE_STATES),
            )
            return cursor.rowcount == 1

    async def set_item_preview(self, row_id: int, message_id: Optional[int]) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                f"UPDATE {ITEMS_TABLE} SET preview_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, now_iso(), row_id),
            )

    # ------------------------------------------------------------------ #
    # Albums
    # ------------------------------------------------------------------ #

    async def upsert_album(
        self,
        *,
        source_chat_id: int,
        media_group_id: str,
        topic_id: int,
        sender_id: int,
        anime_title: str = "",
        emoji: str = "",
    ) -> Tuple[int, bool]:
        """Create the album if new; return ``(album_id, created)``.

        Re-arrivals for a known album never reset its state.
        """
        assert self._conn is not None
        existing = await self.get_album_by_group(source_chat_id, media_group_id)
        if existing is not None:
            return int(existing["id"]), False

        timestamp = now_iso()
        try:
            async with self._transaction() as conn:
                cursor = await conn.execute(
                    f"""INSERT INTO {ALBUMS_TABLE} (
                            source_chat_id, media_group_id, topic_id, sender_id,
                            anime_title, emoji, status, created_at, updated_at, last_item_received_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_chat_id, media_group_id, topic_id, sender_id,
                        anime_title, emoji, Status.COLLECTING, timestamp, timestamp, timestamp,
                    ),
                )
                return int(cursor.lastrowid), True
        except sqlite3.IntegrityError:
            # Lost a race against another update for the same album.
            row = await self.get_album_by_group(source_chat_id, media_group_id)
            if row is None:  # pragma: no cover - defensive
                raise
            return int(row["id"]), False

    async def add_album_item(
        self,
        *,
        album_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: str,
        file_id: str,
        original_caption: Optional[str] = None,
    ) -> bool:
        """Append an album part idempotently.

        Returns ``True`` when the part was newly inserted. Duplicate Telegram
        updates and duplicate parts are rejected by the database.
        """
        assert self._conn is not None
        timestamp = now_iso()
        try:
            async with self._transaction() as conn:
                cursor = await conn.execute(
                    f"SELECT COALESCE(MAX(ordinal), 0) + 1 FROM {ALBUM_ITEMS_TABLE} WHERE album_id = ?",
                    (album_id,),
                )
                row = await cursor.fetchone()
                next_ordinal = int(row[0]) if row else 1

                await conn.execute(
                    f"""INSERT INTO {ALBUM_ITEMS_TABLE} (
                            album_id, source_chat_id, source_message_id, ordinal,
                            media_type, file_id, original_caption, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        album_id, source_chat_id, source_message_id, next_ordinal,
                        media_type, file_id, original_caption, timestamp,
                    ),
                )
                await conn.execute(
                    f"""UPDATE {ALBUMS_TABLE}
                        SET last_item_received_at = ?, item_count = item_count + 1,
                            updated_at = ?, version = version + 1
                        WHERE id = ?""",
                    (timestamp, timestamp, album_id),
                )
            return True
        except sqlite3.IntegrityError:
            logger.info(
                "duplicate-album-part-rejected album=%s chat=%s message=%s",
                album_id, source_chat_id, source_message_id,
            )
            return False

    async def get_album(self, album_id: int) -> Optional[Dict[str, Any]]:
        return await self._fetch_one(f"SELECT * FROM {ALBUMS_TABLE} WHERE id = ?", (album_id,))

    async def get_album_by_group(self, source_chat_id: int, media_group_id: str) -> Optional[Dict[str, Any]]:
        return await self._fetch_one(
            f"SELECT * FROM {ALBUMS_TABLE} WHERE source_chat_id = ? AND media_group_id = ?",
            (source_chat_id, media_group_id),
        )

    async def get_album_items(self, album_id: int) -> List[Dict[str, Any]]:
        """Album parts ordered by source message id (Telegram's authoritative order)."""
        return await self._fetch_all(
            f"SELECT * FROM {ALBUM_ITEMS_TABLE} WHERE album_id = ? ORDER BY source_message_id ASC",
            (album_id,),
        )

    async def renumber_album_items(self, album_id: int) -> None:
        """Rewrite ``ordinal`` to match the authoritative ordering."""
        assert self._conn is not None
        items = await self.get_album_items(album_id)
        async with self._transaction() as conn:
            for index, item in enumerate(items, start=1):
                await conn.execute(
                    f"UPDATE {ALBUM_ITEMS_TABLE} SET ordinal = ? WHERE id = ?",
                    (index, item["id"]),
                )
            await conn.execute(
                f"UPDATE {ALBUMS_TABLE} SET item_count = ?, updated_at = ? WHERE id = ?",
                (len(items), now_iso(), album_id),
            )

    async def get_albums_by_status(self, status: str) -> List[Dict[str, Any]]:
        return await self._fetch_all(
            f"SELECT * FROM {ALBUMS_TABLE} WHERE status = ? ORDER BY id ASC", (status,)
        )

    async def albums_due(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        """Collecting albums whose quiet period has elapsed."""
        return await self._fetch_all(
            f"""SELECT * FROM {ALBUMS_TABLE}
                WHERE status = ? AND last_item_received_at <= ?
                ORDER BY last_item_received_at ASC""",
            (Status.COLLECTING, cutoff_iso),
        )

    async def finish_album_collection(
        self, album_id: int, episode_number: Optional[str], final_caption: str
    ) -> bool:
        """collecting -> pending, recording the resolved episode data."""
        return await self._cas(
            ALBUMS_TABLE, album_id, (Status.COLLECTING,), Status.PENDING,
            {"episode_number": episode_number, "final_caption": final_caption},
        )

    async def claim_album_for_publishing(self, album_id: int) -> bool:
        return await self._cas(
            ALBUMS_TABLE, album_id, PUBLISHABLE_STATES, Status.PUBLISHING,
            {"publishing_started_at": now_iso(), "last_error": None},
        )

    async def finish_album_published(
        self,
        album_id: int,
        *,
        target_chat_id: str,
        target_message_ids: Sequence[int],
        final_caption: str,
    ) -> bool:
        return await self._cas(
            ALBUMS_TABLE, album_id, (Status.PUBLISHING,), Status.PUBLISHED,
            {
                "target_chat_id": str(target_chat_id),
                "target_message_ids": json.dumps([int(m) for m in target_message_ids]),
                "published_at": now_iso(),
                "final_caption": final_caption,
                "uncertain": 0,
                "last_error": None,
            },
        )

    async def finish_album_failed(self, album_id: int, error: str) -> bool:
        return await self._cas(
            ALBUMS_TABLE, album_id, (Status.PUBLISHING,), Status.FAILED,
            {"last_error": error[:500], "uncertain": 0},
        )

    async def mark_album_uncertain(self, album_id: int, reason: str) -> bool:
        return await self._cas(
            ALBUMS_TABLE, album_id, (Status.PUBLISHING,), Status.UNCERTAIN,
            {"last_error": reason[:500], "uncertain": 1},
        )

    async def cancel_album(self, album_id: int) -> bool:
        return await self._cas(
            ALBUMS_TABLE, album_id, CANCELABLE_STATES, Status.CANCELED, {"last_error": None},
        )

    async def resolve_album_uncertain_published(
        self, album_id: int, target_message_ids: Optional[Sequence[int]] = None
    ) -> bool:
        extra: Dict[str, Any] = {
            "uncertain": 0,
            "published_at": now_iso(),
            "last_error": "resolved by admin: confirmed published",
        }
        if target_message_ids:
            extra["target_message_ids"] = json.dumps([int(m) for m in target_message_ids])
        return await self._cas(ALBUMS_TABLE, album_id, (Status.UNCERTAIN,), Status.PUBLISHED, extra)

    async def resolve_album_uncertain_absent(self, album_id: int) -> bool:
        """uncertain -> pending so an admin can retry knowingly."""
        return await self._cas(
            ALBUMS_TABLE, album_id, (Status.UNCERTAIN,), Status.PENDING,
            {"uncertain": 0, "last_error": "resolved by admin: confirmed not published"},
        )

    async def set_album_episode(self, album_id: int, episode_number: str, final_caption: str) -> bool:
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(
                f"UPDATE {ALBUMS_TABLE} SET episode_number = ?, final_caption = ?, updated_at = ?, "
                f"version = version + 1 WHERE id = ? AND status IN ({self._placeholders(EDITABLE_STATES)})",
                (episode_number, final_caption, now_iso(), album_id, *EDITABLE_STATES),
            )
            return cursor.rowcount == 1

    async def set_album_preview(self, album_id: int, message_id: Optional[int]) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                f"UPDATE {ALBUMS_TABLE} SET preview_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, now_iso(), album_id),
            )

    # ------------------------------------------------------------------ #
    # Crash recovery
    # ------------------------------------------------------------------ #

    async def recover_interrupted_publishing(self) -> Dict[str, List[Dict[str, Any]]]:
        """Convert every ``publishing`` row into ``uncertain``.

        At startup no publish attempt can still be in flight, so a ``publishing``
        row means the process died mid-send. Telegram offers no idempotency key,
        therefore the only safe move is to flag it for manual review - never to
        reset it to pending, which would risk a duplicate channel post.
        """
        reason = "interrupted: process stopped during publish, outcome unknown"
        recovered: Dict[str, List[Dict[str, Any]]] = {ITEMS_TABLE: [], ALBUMS_TABLE: []}

        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(
                f"SELECT * FROM {ITEMS_TABLE} WHERE status = ?", (Status.PUBLISHING,)
            )
            recovered[ITEMS_TABLE] = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()

            cursor = await self._conn.execute(
                f"SELECT * FROM {ALBUMS_TABLE} WHERE status = ?", (Status.PUBLISHING,)
            )
            recovered[ALBUMS_TABLE] = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()

        for row in recovered[ITEMS_TABLE]:
            await self.mark_item_uncertain(int(row["id"]), reason)
        for row in recovered[ALBUMS_TABLE]:
            await self.mark_album_uncertain(int(row["id"]), reason)

        total = len(recovered[ITEMS_TABLE]) + len(recovered[ALBUMS_TABLE])
        logger.info(
            "recovery-interrupted items=%d albums=%d",
            len(recovered[ITEMS_TABLE]), len(recovered[ALBUMS_TABLE]),
        )
        if total:
            logger.warning(
                "%d interrupted publish attempt(s) moved to uncertain and require admin review",
                total,
            )
        return recovered

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    async def counts_by_state(self, table: str = ITEMS_TABLE) -> Dict[str, int]:
        if table not in (ITEMS_TABLE, ALBUMS_TABLE):
            raise ValueError(f"unknown table {table!r}")
        rows = await self._fetch_all(
            f"SELECT status, COUNT(*) AS total FROM {table} GROUP BY status"
        )
        counts = {state: 0 for state in ALL_STATES}
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        return counts

    async def pending_count(self) -> int:
        counts = await self.counts_by_state(ITEMS_TABLE)
        albums = await self.counts_by_state(ALBUMS_TABLE)
        return counts.get(Status.PENDING, 0) + albums.get(Status.PENDING, 0)

    async def uncertain_rows(self) -> List[Dict[str, Any]]:
        items = await self._fetch_all(
            f"SELECT * FROM {ITEMS_TABLE} WHERE status = ? ORDER BY id", (Status.UNCERTAIN,)
        )
        for row in items:
            row["kind"] = "item"
        albums = await self._fetch_all(
            f"SELECT * FROM {ALBUMS_TABLE} WHERE status = ? ORDER BY id", (Status.UNCERTAIN,)
        )
        for row in albums:
            row["kind"] = "album"
        return items + albums

    async def health_check(self) -> bool:
        """True when the database answers a trivial query."""
        if self._conn is None:
            return False
        try:
            await self._fetch_one("SELECT 1 AS ok")
            return True
        except sqlite3.Error:
            logger.exception("database-health-check-failed")
            return False


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

_CREATE_MEDIA_ITEMS = f"""
CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    media_group_id TEXT,
    topic_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL DEFAULT '',
    caption TEXT,
    anime_title TEXT,
    emoji TEXT,
    episode_number TEXT,
    status TEXT NOT NULL DEFAULT '{Status.PENDING}',
    uncertain INTEGER NOT NULL DEFAULT 0,
    target_chat_id TEXT,
    target_message_id TEXT,
    published_at TEXT,
    publishing_started_at TEXT,
    final_caption TEXT,
    preview_message_id INTEGER,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_chat_id, source_message_id)
)
"""

_CREATE_ALBUMS = f"""
CREATE TABLE IF NOT EXISTS {ALBUMS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL,
    media_group_id TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    anime_title TEXT,
    emoji TEXT,
    episode_number TEXT,
    final_caption TEXT,
    status TEXT NOT NULL DEFAULT '{Status.COLLECTING}',
    uncertain INTEGER NOT NULL DEFAULT 0,
    expected_size INTEGER,
    item_count INTEGER NOT NULL DEFAULT 0,
    target_chat_id TEXT,
    target_message_ids TEXT,
    published_at TEXT,
    publishing_started_at TEXT,
    preview_message_id INTEGER,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_item_received_at TEXT NOT NULL,
    UNIQUE(source_chat_id, media_group_id)
)
"""

_CREATE_ALBUM_ITEMS = f"""
CREATE TABLE IF NOT EXISTS {ALBUM_ITEMS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL REFERENCES {ALBUMS_TABLE}(id) ON DELETE CASCADE,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    media_type TEXT NOT NULL,
    file_id TEXT NOT NULL,
    original_caption TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(album_id, source_message_id),
    UNIQUE(source_chat_id, source_message_id)
)
"""

_INDEXES: Tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_items_status ON {ITEMS_TABLE}(status)",
    f"CREATE INDEX IF NOT EXISTS idx_items_uncertain ON {ITEMS_TABLE}(status) WHERE uncertain = 1",
    f"CREATE INDEX IF NOT EXISTS idx_items_source_chat ON {ITEMS_TABLE}(source_chat_id)",
    f"CREATE INDEX IF NOT EXISTS idx_items_media_group ON {ITEMS_TABLE}(source_chat_id, media_group_id) "
    f"WHERE media_group_id IS NOT NULL",
    f"CREATE INDEX IF NOT EXISTS idx_albums_status ON {ALBUMS_TABLE}(status)",
    f"CREATE INDEX IF NOT EXISTS idx_albums_due ON {ALBUMS_TABLE}(status, last_item_received_at)",
    f"CREATE INDEX IF NOT EXISTS idx_album_items_album ON {ALBUM_ITEMS_TABLE}(album_id, source_message_id)",
)