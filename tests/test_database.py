"""Database schema, migrations, compare-and-set transitions, recovery (Phase E/G/H/K)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from database import (
    ALBUMS_TABLE,
    ITEMS_TABLE,
    SCHEMA_VERSION,
    Status,
    Database,
    iso,
    now_iso,
)
from tests.conftest import ADMIN_ID, ONE_PIECE_TOPIC, SOURCE_CHAT

# The legacy v1 definition, exactly as shipped by the previous release.
LEGACY_SCHEMA = """
CREATE TABLE media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    media_group_id TEXT,
    topic_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL,
    caption TEXT,
    anime_title TEXT,
    emoji TEXT,
    episode_number TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    target_chat_id TEXT,
    target_message_id TEXT,
    published_at TEXT,
    final_caption TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')),
    UNIQUE(source_chat_id, source_message_id)
)
"""


async def make_item(db, **overrides):
    payload = {
        "source_chat_id": SOURCE_CHAT,
        "source_message_id": 100,
        "topic_id": ONE_PIECE_TOPIC,
        "sender_id": ADMIN_ID,
        "media_type": "video",
        "file_id": "file-abc",
        "file_unique_id": "uniq-abc",
        "caption": "EP 1165",
        "anime_title": "One Piece",
        "emoji": "\U0001F4FA",
        "episode_number": "1165",
    }
    payload.update(overrides)
    return await db.insert_media_item(**payload)


async def make_album(db, group_id="grp-1", **overrides):
    payload = {
        "source_chat_id": SOURCE_CHAT,
        "media_group_id": group_id,
        "topic_id": ONE_PIECE_TOPIC,
        "sender_id": ADMIN_ID,
        "anime_title": "One Piece",
        "emoji": "\U0001F4FA",
    }
    payload.update(overrides)
    return await db.upsert_album(**payload)


# --------------------------------------------------------------------------- #
# Initialization / shutdown
# --------------------------------------------------------------------------- #


async def test_connect_initializes_schema(db):
    assert db.is_connected is True
    assert db.schema_version == SCHEMA_VERSION


async def test_close_clears_connection(db_path):
    database = Database(db_path)
    await database.connect()
    assert database.is_connected is True
    await database.close()
    assert database.is_connected is False


async def test_connect_is_idempotent(db):
    await db.connect()
    assert db.is_connected is True


async def test_foreign_keys_are_enforced(db):
    assert await db.health_check() is True
    with pytest.raises(sqlite3.IntegrityError):
        await db._conn.execute(
            "INSERT INTO album_items (album_id, source_chat_id, source_message_id, "
            "ordinal, media_type, file_id, created_at) VALUES (9999, 1, 1, 1, 'photo', 'f', 'x')"
        )


async def test_no_pass_stubs_remain():
    """Phase E.8: fake migration functions must not exist."""
    from pathlib import Path

    import database as database_module

    source = Path(database_module.__file__).read_text(encoding="utf-8")
    assert [line.strip() for line in source.splitlines() if line.strip() == "pass"] == []


async def test_now_iso_is_fixed_width_for_lexicographic_comparison():
    """Values are compared with string SQL comparisons, so the width is fixed."""
    value = now_iso()
    assert value.endswith("+00:00")
    assert "." in value
    assert len(value) == len(now_iso())


async def test_iso_is_fixed_width_for_a_past_timestamp():
    from datetime import datetime, timezone

    value = iso(datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert value == "2020-01-02T03:04:05.000000+00:00"


# --------------------------------------------------------------------------- #
# Migrations (Phase E.10)
# --------------------------------------------------------------------------- #


async def test_migration_from_empty_database(tmp_path):
    path = str(tmp_path / "empty.db")
    database = Database(path)
    await database.connect()
    assert database.schema_version == SCHEMA_VERSION
    cursor = await database._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (ALBUMS_TABLE,)
    )
    assert await cursor.fetchone() is not None
    await database.close()


async def test_migration_from_legacy_schema(tmp_path):
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.commit()
    raw.close()

    database = Database(path)
    await database.connect()
    assert database.schema_version == SCHEMA_VERSION
    columns = {row["name"] for row in await (await database._conn.execute(
        "PRAGMA table_info(media_items)"
    )).fetchall()}
    for added in ("uncertain", "publishing_started_at", "preview_message_id", "last_error", "version"):
        assert added in columns
    await database.close()


async def test_migration_preserves_existing_records(tmp_path):
    path = str(tmp_path / "legacy-data.db")
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute(
        "INSERT INTO media_items (source_chat_id, source_message_id, topic_id, sender_id, "
        "media_type, file_id, file_unique_id, status, anime_title, episode_number) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SOURCE_CHAT, 777, ONE_PIECE_TOPIC, ADMIN_ID, "video", "legacy-file", "legacy-uniq",
         "published", "One Piece", "900"),
    )
    raw.commit()
    raw.close()

    database = Database(path)
    await database.connect()
    row = await database.get_item_by_source(SOURCE_CHAT, 777)
    assert row is not None
    assert row["status"] == "published"
    assert row["file_id"] == "legacy-file"
    assert row["episode_number"] == "900"
    # New columns get safe defaults for migrated rows.
    assert row["uncertain"] == 0
    assert row["version"] == 1
    await database.close()


async def test_repeated_initialization_is_safe_and_lossless(tmp_path):
    path = str(tmp_path / "repeat.db")
    first = Database(path)
    await first.connect()
    await make_item(first, source_message_id=1)
    await make_item(first, source_message_id=2)
    await first.close()

    for _ in range(3):
        database = Database(path)
        await database.connect()
        assert database.schema_version == SCHEMA_VERSION
        await database.close()

    final = Database(path)
    await final.connect()
    assert await final.get_item_by_source(SOURCE_CHAT, 1) is not None
    assert await final.get_item_by_source(SOURCE_CHAT, 2) is not None
    await final.close()


async def test_legacy_unique_index_survives_migration(tmp_path):
    path = str(tmp_path / "uniq.db")
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.commit()
    raw.close()

    database = Database(path)
    await database.connect()
    inserted, _ = await make_item(database, source_message_id=5)
    duplicate, _ = await make_item(database, source_message_id=5)
    assert inserted is True and duplicate is False
    await database.close()


# --------------------------------------------------------------------------- #
# Duplicate prevention (Phase K)
# --------------------------------------------------------------------------- #


async def test_duplicate_single_message_rejected(db):
    assert (await make_item(db))[0] is True
    assert (await make_item(db))[0] is False


async def test_same_message_id_in_a_different_chat_is_allowed(db):
    await make_item(db, source_chat_id=-1001, source_message_id=1)
    await make_item(db, source_chat_id=-1002, source_message_id=1)
    assert await db.get_item_by_source(-1001, 1) is not None
    assert await db.get_item_by_source(-1002, 1) is not None


async def test_concurrent_duplicate_inserts_produce_one_row(db):
    results = await asyncio.gather(
        *[make_item(db, source_message_id=42) for _ in range(12)]
    )
    inserted = [row_id for inserted, row_id in results if inserted]
    assert len(inserted) == 1
    counts = await db.counts_by_state(ITEMS_TABLE)
    assert counts[Status.PENDING] == 1


async def test_duplicate_album_rejected(db):
    album_id, created = await make_album(db)
    again_id, created_again = await make_album(db)
    assert created is True and created_again is False
    assert album_id == again_id


async def test_duplicate_album_part_rejected(db):
    album_id, _ = await make_album(db)
    first = await db.add_album_item(
        album_id=album_id, source_chat_id=SOURCE_CHAT, source_message_id=1,
        media_type="photo", file_id="a")
    second = await db.add_album_item(
        album_id=album_id, source_chat_id=SOURCE_CHAT, source_message_id=1,
        media_type="photo", file_id="b")
    assert first is True and second is False
    items = await db.get_album_items(album_id)
    assert len(items) == 1
    assert items[0]["file_id"] == "a"


async def test_album_part_cannot_be_shared_across_albums(db):
    first_album, _ = await make_album(db, group_id="grp-a")
    second_album, _ = await make_album(db, group_id="grp-b")
    await db.add_album_item(
        album_id=first_album, source_chat_id=SOURCE_CHAT, source_message_id=9,
        media_type="photo", file_id="a")
    added = await db.add_album_item(
        album_id=second_album, source_chat_id=SOURCE_CHAT, source_message_id=9,
        media_type="photo", file_id="b")
    assert added is False


async def test_concurrent_duplicate_album_creation(db):
    results = await asyncio.gather(*[make_album(db, group_id="race") for _ in range(10)])
    ids = {album_id for album_id, _ in results}
    assert len(ids) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_concurrent_duplicate_album_parts(db):
    album_id, _ = await make_album(db, group_id="grp-race")
    results = await asyncio.gather(*[
        db.add_album_item(
            album_id=album_id, source_chat_id=SOURCE_CHAT, source_message_id=7,
            media_type="photo", file_id=f"f{i}")
        for i in range(10)
    ])
    assert sum(1 for added in results if added) == 1
    assert len(await db.get_album_items(album_id)) == 1


# --------------------------------------------------------------------------- #
# Album ordering (Phase F.4)
# --------------------------------------------------------------------------- #


async def test_album_items_are_ordered_by_source_message_id(db):
    album_id, _ = await make_album(db)
    for message_id in (30, 10, 20):
        await db.add_album_item(
            album_id=album_id, source_chat_id=SOURCE_CHAT, source_message_id=message_id,
            media_type="photo", file_id=f"f{message_id}")
    await db.renumber_album_items(album_id)
    items = await db.get_album_items(album_id)
    assert [i["source_message_id"] for i in items] == [10, 20, 30]
    assert [i["ordinal"] for i in items] == [1, 2, 3]


async def test_album_item_count_is_tracked(db):
    album_id, _ = await make_album(db)
    for message_id in (1, 2, 3):
        await db.add_album_item(
            album_id=album_id, source_chat_id=SOURCE_CHAT, source_message_id=message_id,
            media_type="photo", file_id="f")
    album = await db.get_album(album_id)
    assert album["item_count"] == 3
    assert album["last_item_received_at"]


# --------------------------------------------------------------------------- #
# Compare-and-set state machine (Phase G)
# --------------------------------------------------------------------------- #


async def test_item_happy_path(db):
    _, row_id = await make_item(db)
    assert await db.claim_item_for_publishing(row_id) is True
    assert await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[42], final_caption="cap"
    ) is True
    row = await db.get_item(row_id)
    assert row["status"] == Status.PUBLISHED
    assert row["target_message_id"] == "42"
    assert row["target_chat_id"] == "@aniwavebd"
    assert row["published_at"]
    assert row["final_caption"] == "cap"


async def test_only_one_concurrent_publish_claim_wins(db):
    _, row_id = await make_item(db)
    results = await asyncio.gather(
        *[db.claim_item_for_publishing(row_id) for _ in range(20)]
    )
    assert sum(1 for claimed in results if claimed) == 1


async def test_published_item_cannot_be_claimed_again(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption=""
    )
    assert await db.claim_item_for_publishing(row_id) is False


async def test_canceled_item_cannot_be_claimed(db):
    _, row_id = await make_item(db)
    assert await db.cancel_item(row_id) is True
    assert await db.claim_item_for_publishing(row_id) is False


async def test_uncertain_item_cannot_be_claimed(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    assert await db.claim_item_for_publishing(row_id) is False


async def test_failed_item_can_be_reclaimed(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_failed(row_id, "rate limited")
    assert await db.claim_item_for_publishing(row_id) is True


async def test_collecting_album_cannot_be_cancelled_from_wrong_state(db):
    album_id, _ = await make_album(db)
    assert await db.cancel_album(album_id) is False
    await db.finish_album_collection(album_id, "1165", "cap")
    assert await db.cancel_album(album_id) is True


async def test_album_collecting_to_pending_is_atomic(db):
    album_id, _ = await make_album(db)
    results = await asyncio.gather(
        *[db.finish_album_collection(album_id, "1165", "cap") for _ in range(10)]
    )
    assert sum(1 for ok in results if ok) == 1


async def test_album_publish_lifecycle(db):
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "1165", "cap")
    assert await db.claim_album_for_publishing(album_id) is True
    assert await db.finish_album_published(
        album_id, target_chat_id="@aniwavebd", target_message_ids=[1, 2], final_caption="cap"
    ) is True
    album = await db.get_album(album_id)
    assert album["status"] == Status.PUBLISHED
    assert album["target_message_ids"] == "[1, 2]"


async def test_episode_editable_only_from_allowed_states(db):
    _, row_id = await make_item(db)
    assert await db.set_item_episode(row_id, "2000", "cap") is True
    await db.claim_item_for_publishing(row_id)
    assert await db.set_item_episode(row_id, "3000", "cap") is False
    await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption="c"
    )
    assert await db.set_item_episode(row_id, "4000", "cap") is False


async def test_album_episode_editable_only_from_allowed_states(db):
    album_id, _ = await make_album(db)
    assert await db.set_album_episode(album_id, "5", "cap") is False
    await db.finish_album_collection(album_id, "5", "cap")
    assert await db.set_album_episode(album_id, "6", "cap") is True
    await db.claim_album_for_publishing(album_id)
    assert await db.set_album_episode(album_id, "7", "cap") is False


async def test_version_increments_on_transitions(db):
    _, row_id = await make_item(db)
    before = (await db.get_item(row_id))["version"]
    await db.claim_item_for_publishing(row_id)
    after = (await db.get_item(row_id))["version"]
    assert after == before + 1


# --------------------------------------------------------------------------- #
# Crash recovery (Phase H)
# --------------------------------------------------------------------------- #


async def test_recovery_converts_publishing_to_uncertain(db):
    _, item_id = await make_item(db)
    await db.claim_item_for_publishing(item_id)
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "7", "cap")
    await db.claim_album_for_publishing(album_id)

    recovered = await db.recover_interrupted_publishing()
    assert len(recovered[ITEMS_TABLE]) == 1
    assert len(recovered[ALBUMS_TABLE]) == 1

    item = await db.get_item(item_id)
    assert item["status"] == Status.UNCERTAIN
    assert item["uncertain"] == 1
    assert "interrupted" in item["last_error"]
    assert item["publishing_started_at"]

    album = await db.get_album(album_id)
    assert album["status"] == Status.UNCERTAIN
    assert album["uncertain"] == 1


async def test_recovery_never_resets_publishing_to_pending(db):
    _, item_id = await make_item(db)
    await db.claim_item_for_publishing(item_id)
    await db.recover_interrupted_publishing()
    assert (await db.get_item(item_id))["status"] != Status.PENDING


async def test_recovery_is_idempotent(db):
    _, item_id = await make_item(db)
    await db.claim_item_for_publishing(item_id)
    first = await db.recover_interrupted_publishing()
    second = await db.recover_interrupted_publishing()
    assert len(first[ITEMS_TABLE]) == 1
    assert len(second[ITEMS_TABLE]) == 0


async def test_recovery_leaves_other_states_untouched(db):
    _, published_id = await make_item(db, source_message_id=1)
    await db.claim_item_for_publishing(published_id)
    await db.finish_item_published(
        published_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption=""
    )
    _, pending_id = await make_item(db, source_message_id=2)
    await db.recover_interrupted_publishing()
    assert (await db.get_item(published_id))["status"] == Status.PUBLISHED
    assert (await db.get_item(pending_id))["status"] == Status.PENDING


async def test_restart_cannot_turn_published_into_pending(tmp_path):
    path = str(tmp_path / "restart.db")
    first = Database(path)
    await first.connect()
    _, row_id = await make_item(first)
    await first.claim_item_for_publishing(row_id)
    await first.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[5], final_caption="cap"
    )
    await first.close()

    second = Database(path)
    await second.connect()
    await second.recover_interrupted_publishing()
    row = await second.get_item(row_id)
    assert row["status"] == Status.PUBLISHED
    assert row["target_message_id"] == "5"
    await second.close()


# --------------------------------------------------------------------------- #
# Uncertain resolution
# --------------------------------------------------------------------------- #


async def test_resolve_uncertain_published(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    assert await db.resolve_item_uncertain_published(row_id, target_message_ids=[77]) is True
    row = await db.get_item(row_id)
    assert row["status"] == Status.PUBLISHED
    assert row["target_message_id"] == "77"
    assert row["uncertain"] == 0


async def test_resolve_uncertain_absent_makes_item_retryable(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    assert await db.resolve_item_uncertain_absent(row_id) is True
    assert (await db.get_item(row_id))["status"] == Status.FAILED
    assert await db.claim_item_for_publishing(row_id) is True


async def test_resolve_uncertain_absent_makes_album_pending(db):
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "5", "cap")
    await db.claim_album_for_publishing(album_id)
    await db.mark_album_uncertain(album_id, "crash")
    assert await db.resolve_album_uncertain_absent(album_id) is True
    assert (await db.get_album(album_id))["status"] == Status.PENDING


async def test_resolution_rejected_from_wrong_state(db):
    _, row_id = await make_item(db)
    assert await db.resolve_item_uncertain_published(row_id) is False
    assert await db.resolve_item_uncertain_absent(row_id) is False


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


async def test_counts_by_state_covers_every_state(db):
    counts = await db.counts_by_state(ITEMS_TABLE)
    for state in (
        Status.COLLECTING, Status.PENDING, Status.PUBLISHING, Status.PUBLISHED,
        Status.FAILED, Status.UNCERTAIN, Status.CANCELED,
    ):
        assert state in counts


async def test_pending_count_includes_albums(db):
    await make_item(db, source_message_id=1)
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "5", "cap")
    assert await db.pending_count() == 2


async def test_uncertain_rows_report_kind(db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "5", "cap")
    await db.claim_album_for_publishing(album_id)
    await db.mark_album_uncertain(album_id, "crash")
    rows = await db.uncertain_rows()
    assert {row["kind"] for row in rows} == {"item", "album"}
    assert {int(row["id"]) for row in rows} == {row_id, album_id}


async def test_albums_due_uses_quiet_period(db):
    """An album is due once ``last_item_received_at <= cutoff``."""
    from datetime import datetime, timedelta, timezone

    album_id, _ = await make_album(db)

    # A cutoff after the arrival time means the album has settled.
    future_cutoff = iso(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert [int(row["id"]) for row in await db.albums_due(future_cutoff)] == [album_id]

    # A cutoff before the arrival time means it is still collecting.
    past_cutoff = iso(datetime.now(timezone.utc) - timedelta(days=1))
    assert await db.albums_due(past_cutoff) == []


async def test_counts_rejects_unknown_table(db):
    with pytest.raises(ValueError):
        await db.counts_by_state("not_a_table")


async def test_database_path_is_not_leaked_in_logs(db, caplog):
    """Only the file name may be logged, never the full local path."""
    import logging

    probe = Database(str(db.path))
    with caplog.at_level(logging.INFO, logger="database"):
        await probe.connect()
        try:
            assert str(db.path) not in caplog.text
            assert "database-ready" in caplog.text
        finally:
            await probe.close()