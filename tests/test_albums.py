"""Album collection, quiet-period finalization and restart recovery (Phase F / N 14-16)."""

from __future__ import annotations

import asyncio

import pytest

from albums import RECONCILE_INTERVAL_SECONDS, AlbumPart, AlbumService, resolve_album_episode
from database import Status, iso
from tests.conftest import ADMIN_ID, ONE_PIECE_TOPIC, SOURCE_CHAT


def part(**overrides) -> AlbumPart:
    payload = {
        "source_chat_id": SOURCE_CHAT,
        "source_message_id": 1,
        "media_group_id": "grp-1",
        "topic_id": ONE_PIECE_TOPIC,
        "sender_id": ADMIN_ID,
        "media_type": "photo",
        "file_id": "f1",
        "caption": "One Piece EP 1165",
    }
    payload.update(overrides)
    return AlbumPart(**payload)


# --------------------------------------------------------------------------- #
# Ingestion (Phase F.1-3, 10)
# --------------------------------------------------------------------------- #


async def test_single_part_creates_album(album_service, db):
    album_id = await album_service.ingest_part(part(), "One Piece", "\U0001F4FA")
    album = await db.get_album(album_id)
    assert album["status"] == Status.COLLECTING
    assert album["media_group_id"] == "grp-1"
    assert album["item_count"] == 1
    assert album["last_item_received_at"]


async def test_duplicate_updates_do_not_duplicate_parts(album_service, db):
    album_id = await album_service.ingest_part(part(), "One Piece", "\U0001F4FA")
    for _ in range(5):
        await album_service.ingest_part(part(), "One Piece", "\U0001F4FA")
    assert len(await db.get_album_items(album_id)) == 1


async def test_parts_grouped_under_one_album(album_service, db):
    album_id = await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    for message_id in (2, 3):
        await album_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    assert len(await db.get_album_items(album_id)) == 3


async def test_different_groups_create_different_albums(album_service, db):
    first = await album_service.ingest_part(part(media_group_id="grp-a"), "One Piece", "\U0001F4FA")
    second = await album_service.ingest_part(part(media_group_id="grp-b"), "One Piece", "\U0001F4FA")
    assert first != second


async def test_album_identity_survives_reingest(album_service, db):
    album_id = await album_service.ingest_part(part(), "One Piece", "\U0001F4FA")
    same = await album_service.ingest_part(part(source_message_id=2), "One Piece", "\U0001F4FA")
    assert same == album_id


# --------------------------------------------------------------------------- #
# Ordering (Phase F.4)
# --------------------------------------------------------------------------- #


async def test_out_of_order_arrival_is_ordered_by_message_id(album_service, db):
    album_id = await album_service.ingest_part(part(source_message_id=30), "One Piece", "\U0001F4FA")
    for message_id in (10, 20):
        await album_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    await db.renumber_album_items(album_id)
    items = await db.get_album_items(album_id)
    assert [i["source_message_id"] for i in items] == [10, 20, 30]


async def test_finalization_renumbers_ordinals(album_service, db, clock):
    album_id = await album_service.ingest_part(part(source_message_id=30), "One Piece", "\U0001F4FA")
    for message_id in (10, 20):
        await album_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    clock.advance(60)
    await album_service.finalize_album(album_id)
    items = await db.get_album_items(album_id)
    assert [i["ordinal"] for i in items] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Quiet period (Phase F.7, 16, 17)
# --------------------------------------------------------------------------- #


def test_quiet_period_is_a_named_constant():
    assert isinstance(RECONCILE_INTERVAL_SECONDS, float)
    assert RECONCILE_INTERVAL_SECONDS > 0


async def test_album_not_finalized_before_quiet_period(album_service, db, clock, fake_bot):
    await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    clock.advance(album_service.config.album_quiet_seconds - 1)
    assert await album_service.finalize_due() == []
    assert (await db.get_albums_by_status(Status.COLLECTING))


async def test_album_finalized_after_quiet_period(album_service, db, clock):
    album_id = await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    clock.advance(album_service.config.album_quiet_seconds + 1)
    results = await album_service.finalize_due()
    assert [r.album_id for r in results] == [album_id]
    assert (await db.get_album(album_id))["status"] == Status.PENDING


async def test_quiet_period_is_configurable(env, tmp_path, db, publisher, fake_bot, clock):
    from config import AppConfig

    values = dict(env)
    values["ALBUM_QUIET_SECONDS"] = "10"
    config = AppConfig.load(values)
    service = AlbumService(db, publisher, fake_bot, config, now_fn=clock)
    album_id = await service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")

    clock.advance(5)
    assert await service.finalize_due() == []

    clock.advance(20)
    assert [r.album_id for r in await service.finalize_due()] == [album_id]


async def test_album_never_published_more_than_once(album_service, db, clock, auto_config, fake_bot):
    service = AlbumService(db, album_service.publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    clock.advance(60)
    await service.finalize_due()
    await service.finalize_due()
    await service.finalize_due()
    assert len(fake_bot.album_calls) == 1


# --------------------------------------------------------------------------- #
# Validation before pending (Phase F.12)
# --------------------------------------------------------------------------- #


async def test_topic_must_still_be_configured(album_service, db, clock, env):
    from config import AppConfig

    reconfigured = dict(env)
    reconfigured["ONE_PIECE_TOPIC_ID"] = "77"
    config = AppConfig.load(reconfigured)
    service = AlbumService(db, album_service.publisher, album_service.bot, config, now_fn=clock)
    album_id = await service.ingest_part(part(), "One Piece", "\U0001F4FA")
    clock.advance(60)
    results = await service.finalize_album(album_id)
    assert results.action == "skipped"
    assert "topic" in results.reason
    assert (await db.get_album(album_id))["status"] == Status.COLLECTING


async def test_sender_must_still_be_an_admin(album_service, db, clock, env):
    from config import AppConfig

    reconfigured = dict(env)
    reconfigured["ADMIN_IDS"] = "111"
    config = AppConfig.load(reconfigured)
    service = AlbumService(db, album_service.publisher, album_service.bot, config, now_fn=clock)
    album_id = await service.ingest_part(part(), "One Piece", "\U0001F4FA")
    clock.advance(60)
    results = await service.finalize_album(album_id)
    assert results.action == "skipped"
    assert "admin" in results.reason


async def test_source_chat_must_match_configuration(album_service, db, clock, env):
    from config import AppConfig

    reconfigured = dict(env)
    reconfigured["SOURCE_GROUP_ID"] = "-1009999999999"
    config = AppConfig.load(reconfigured)
    service = AlbumService(db, album_service.publisher, album_service.bot, config, now_fn=clock)
    album_id = await service.ingest_part(part(), "One Piece", "\U0001F4FA")
    clock.advance(60)
    results = await service.finalize_album(album_id)
    assert results.action == "skipped"
    assert "source chat" in results.reason


async def test_unsupported_media_type_skipped(album_service, db, clock):
    album_id = await album_service.ingest_part(
        part(media_type="sticker"), "One Piece", "\U0001F4FA"
    )
    clock.advance(60)
    results = await album_service.finalize_album(album_id)
    assert results.action == "failed"
    assert "unsupported" in results.reason


async def test_album_without_parts_is_skipped(album_service, db):
    album_id, _ = await db.upsert_album(
        source_chat_id=SOURCE_CHAT, media_group_id="empty", topic_id=ONE_PIECE_TOPIC,
        sender_id=ADMIN_ID,
    )
    results = await album_service.finalize_album(album_id)
    assert results.action == "skipped"


async def test_already_finalized_album_is_skipped(album_service, db, clock):
    album_id = await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    clock.advance(60)
    await album_service.finalize_album(album_id)
    results = await album_service.finalize_album(album_id)
    assert results.action == "skipped"
    assert "pending" in results.reason


# --------------------------------------------------------------------------- #
# Concurrent finalization (Phase F.11)
# --------------------------------------------------------------------------- #


async def test_two_collectors_cannot_finalize_the_same_album(album_service, db, clock):
    album_id = await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    clock.advance(60)
    results = await asyncio.gather(
        *[album_service.finalize_album(album_id) for _ in range(8)]
    )
    acted = [r for r in results if r.action == "pending"]
    assert len(acted) == 1
    assert (await db.get_album(album_id))["status"] == Status.PENDING


# --------------------------------------------------------------------------- #
# Approval vs automatic mode (Phase F.13-14)
# --------------------------------------------------------------------------- #


async def test_approval_mode_sends_single_album_preview(album_service, db, clock, fake_bot):
    for message_id in (1, 2, 3):
        await album_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    clock.advance(60)
    await album_service.finalize_due()

    previews = [m for m in fake_bot.sent_messages if "Pending approval" in m["text"]]
    assert len(previews) == 1
    assert "Album parts: 3" in previews[0]["text"]
    assert previews[0]["message_thread_id"] == ONE_PIECE_TOPIC
    assert fake_bot.album_calls == []


async def test_album_preview_id_persisted(album_service, db, clock, fake_bot):
    album_id = await album_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    for message_id in (2, 3):
        await album_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    clock.advance(60)
    await album_service.finalize_due()
    album = await db.get_album(album_id)
    assert album["preview_message_id"]


async def test_auto_mode_publishes_after_finalization(db, publisher, fake_bot, clock, auto_config):
    service = AlbumService(db, publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")

    assert fake_bot.album_calls == [], "must not publish while parts are still arriving"
    clock.advance(60)
    results = await service.finalize_due()
    assert results[0].action == "published"
    assert len(fake_bot.album_calls) == 1
    assert (await db.get_album(results[0].album_id))["status"] == Status.PUBLISHED


async def test_auto_mode_without_episode_does_not_publish(db, publisher, fake_bot, clock, auto_config):
    service = AlbumService(db, publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2):
        await service.ingest_part(
            part(source_message_id=message_id, caption="no numbers"), "One Piece", "\U0001F4FA"
        )
    clock.advance(60)
    results = await service.finalize_due()
    assert fake_bot.album_calls == []
    assert results[0].action == "pending"
    assert any("episode number" in m["text"].lower() for m in fake_bot.sent_messages)


async def test_missing_episode_sends_admin_guidance(album_service, db, clock, fake_bot):
    for message_id in (1, 2):
        await album_service.ingest_part(
            part(source_message_id=message_id, caption="no numbers"), "One Piece", "\U0001F4FA"
        )
    clock.advance(60)
    await album_service.finalize_due()
    assert any("Edit Episode" in m["text"] for m in fake_bot.sent_messages)


# --------------------------------------------------------------------------- #
# Restart recovery (Phase F.9)
# --------------------------------------------------------------------------- #


async def test_restart_finalizes_album_that_collected_before_the_crash(tmp_path, db, publisher,
                                                                    fake_bot, clock, config):
    path = str(tmp_path / "restart.db")

    first_db = __import__("database").Database(path)
    await first_db.connect()
    first_service = AlbumService(first_db, publisher, fake_bot, config, now_fn=clock)
    for message_id in (1, 2, 3):
        await first_service.ingest_part(part(source_message_id=message_id), "One Piece", "\U0001F4FA")
    await first_db.close()

    from database import Database

    second_db = Database(path)
    await second_db.connect()
    second_service = AlbumService(second_db, publisher, fake_bot, config, now_fn=clock)

    collecting = await second_db.get_albums_by_status(Status.COLLECTING)
    assert len(collecting) == 1, "album must survive the restart in SQLite"

    clock.advance(3600)
    results = await second_service.recover_on_startup()
    assert len(results) == 1
    assert results[0].action == "pending"
    await second_db.close()


async def test_restart_defers_album_still_inside_quiet_period(tmp_path, publisher, fake_bot,
                                                              clock, config):
    path = str(tmp_path / "restart2.db")
    from database import Database

    first_db = Database(path)
    await first_db.connect()
    first_service = AlbumService(first_db, publisher, fake_bot, config, now_fn=clock)
    await first_service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    await first_db.close()

    second_db = Database(path)
    await second_db.connect()
    second_service = AlbumService(second_db, publisher, fake_bot, config, now_fn=clock)
    results = await second_service.recover_on_startup()
    assert results == []
    assert await second_db.get_albums_by_status(Status.COLLECTING)
    await second_db.close()


async def test_recovery_with_nothing_collecting(album_service):
    assert await album_service.recover_on_startup() == []


# --------------------------------------------------------------------------- #
# Episode resolution across parts
# --------------------------------------------------------------------------- #


def test_resolve_episode_scans_all_parts():
    items = [
        {"original_caption": None},
        {"original_caption": "One Piece EP 1165"},
    ]
    assert resolve_album_episode(items) == "1165"


def test_resolve_episode_returns_none_without_captions():
    assert resolve_album_episode([{"original_caption": None}, {"original_caption": ""}]) is None


def test_resolve_episode_finds_caption_on_last_part():
    items = [{"original_caption": None}, {"original_caption": "Episode 25"}]
    assert resolve_album_episode(items) == "25"


# --------------------------------------------------------------------------- #
# Background loop lifecycle
# --------------------------------------------------------------------------- #


async def test_reconciler_loop_publishes_due_albums(db, publisher, fake_bot, clock, config):
    service = AlbumService(db, publisher, fake_bot, config, now_fn=clock)
    await service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    await service.ingest_part(part(source_message_id=2), "One Piece", "\U0001F4FA")
    clock.advance(60)

    task = service.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.02)
            counts = await db.counts_by_state("albums")
            if counts[Status.PENDING]:
                break
        counts = await db.counts_by_state("albums")
        assert counts[Status.PENDING] == 1
    finally:
        await service.stop()
    assert task.cancelled() or task.done()


async def test_start_is_idempotent(album_service):
    first = album_service.start()
    second = album_service.start()
    assert first is second
    await album_service.stop()


async def test_stop_is_safe_without_start(album_service):
    await album_service.stop()
    await album_service.stop()


async def test_notification_failure_does_not_raise(db, publisher, fake_bot, clock, config):
    fake_bot.send_error = RuntimeError("network down")
    service = AlbumService(db, publisher, fake_bot, config, now_fn=clock)
    await service.ingest_part(part(source_message_id=1), "One Piece", "\U0001F4FA")
    clock.advance(60)
    results = await service.finalize_due()
    assert results[0].action == "pending"