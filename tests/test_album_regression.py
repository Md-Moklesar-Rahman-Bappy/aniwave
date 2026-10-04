"""Album regression coverage after the media-representation change.

Phase 9 requires confirming album handling did not regress, including which
album media combinations Telegram actually permits.
"""

from __future__ import annotations

import pytest

from albums import AlbumPart
from database import Status
from publisher import AlbumValidationError, build_album_media, validate_album_items
from tests.conftest import (
    ADMIN_ID,
    SOURCE_CHAT,
    WEB_SERIES_TOPIC,
    attach_fake_bot,
    build_document_update,
)
from tests.test_handlers import FakeContext


def doc_part(message_id: int, group_id: str = "grp-docs", caption=None) -> AlbumPart:
    return AlbumPart(
        source_chat_id=SOURCE_CHAT,
        source_message_id=message_id,
        media_group_id=group_id,
        topic_id=WEB_SERIES_TOPIC,
        sender_id=ADMIN_ID,
        media_type="document",
        file_id=f"docfile{message_id}",
        caption=caption,
    )


# --------------------------------------------------------------------------- #
# Document parts reach the album collector
# --------------------------------------------------------------------------- #


async def test_document_album_parts_are_persisted(album_service, db):
    album_id = await album_service.ingest_part(
        doc_part(1, caption="Suits EP 1"), "Web Series", "\U0001F4C1"
    )
    for message_id in (2, 3):
        await album_service.ingest_part(doc_part(message_id), "Web Series", "\U0001F4C1")

    album = await db.get_album(album_id)
    assert album["status"] == Status.COLLECTING
    items = await db.get_album_items(album_id)
    assert [i["source_message_id"] for i in items] == [1, 2, 3]
    assert all(i["media_type"] == "document" for i in items)


async def test_document_album_parts_are_idempotent(album_service, db):
    album_id = await album_service.ingest_part(doc_part(1), "Web Series", "\U0001F4C1")
    for _ in range(4):
        await album_service.ingest_part(doc_part(1), "Web Series", "\U0001F4C1")
    assert len(await db.get_album_items(album_id)) == 1


async def test_document_album_is_published_once(album_service, db, clock, auto_config, fake_bot):
    service = type(album_service)(db, album_service.publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await service.ingest_part(
            doc_part(message_id, caption="Suits EP 1" if message_id == 1 else None),
            "Web Series", "\U0001F4C1",
        )
    clock.advance(60)
    await service.finalize_due()
    await service.finalize_due()

    assert len(fake_bot.album_calls) == 1, "an album publishes exactly once"
    album = await db.get_albums_by_status(Status.PUBLISHED)
    assert len(album) == 1


async def test_caption_only_on_first_album_item(album_service, db, clock, auto_config, fake_bot):
    service = type(album_service)(db, album_service.publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await service.ingest_part(
            doc_part(message_id, caption="Suits EP 1" if message_id == 1 else None),
            "Web Series", "\U0001F4C1",
        )
    clock.advance(60)
    await service.finalize_due()

    media = fake_bot.album_calls[0]["media"]
    assert len(media) == 3
    assert media[0].caption and "Episode: 1" in media[0].caption
    assert media[1].caption is None
    assert media[2].caption is None


async def test_album_ordering_survives_out_of_order_arrival(album_service, db, clock):
    album_id = await album_service.ingest_part(doc_part(30), "Web Series", "\U0001F4C1")
    for message_id in (10, 20):
        await album_service.ingest_part(doc_part(message_id), "Web Series", "\U0001F4C1")
    clock.advance(60)
    await service_finalize(album_service, album_id)
    items = await db.get_album_items(album_id)
    assert [i["source_message_id"] for i in items] == [10, 20, 30]
    assert [i["ordinal"] for i in items] == [1, 2, 3]


async def service_finalize(album_service, album_id):
    return await album_service.finalize_album(album_id)


async def test_album_survives_restart(tmp_path, fake_bot, clock, auto_config):
    """A restart must recover the album from SQLite alone.

    Each run uses its own Database *and* Publisher so the components always share
    one connection, exactly as ``build_application`` wires them.
    """
    from albums import AlbumService
    from database import Database
    from publisher import Publisher

    path = str(tmp_path / "album-restart.db")

    first_db = Database(path)
    await first_db.connect()
    first_publisher = Publisher(fake_bot, first_db, auto_config)
    first_service = AlbumService(first_db, first_publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await first_service.ingest_part(
            doc_part(
                message_id,
                "grp-restart",
                caption="Suits EP 1" if message_id == 1 else None,
            ),
            "Web Series", "\U0001F4C1",
        )
    assert await first_db.get_albums_by_status(Status.COLLECTING)
    await first_db.close()

    second_db = Database(path)
    await second_db.connect()
    try:
        assert await second_db.get_albums_by_status(Status.COLLECTING), (
            "album parts are persisted in SQLite, not held in memory"
        )
        second_publisher = Publisher(fake_bot, second_db, auto_config)
        recovered = AlbumService(second_db, second_publisher, fake_bot, auto_config, now_fn=clock)
        clock.advance(3600)
        results = await recovered.recover_on_startup()
        assert len(results) == 1
        assert results[0].action == "published"
        assert len(fake_bot.album_calls) == 1
        assert len(await second_db.get_albums_by_status(Status.PUBLISHED)) == 1
    finally:
        await second_db.close()


async def test_interrupted_album_publish_becomes_uncertain(album_service, db):
    """A timeout mid-send must become uncertain, never a retryable failure."""
    from telegram.error import TimedOut

    album_id = await album_service.ingest_part(
        doc_part(1, "grp-uncertain", caption="Suits EP 1"), "Web Series", "\U0001F4C1"
    )
    await album_service.ingest_part(doc_part(2, "grp-uncertain"), "Web Series", "\U0001F4C1")
    await db.finish_album_collection(album_id, "1", "cap")
    assert (await db.get_album(album_id))["status"] == Status.PENDING

    album_service.publisher.bot.album_error = TimedOut()
    outcome = await album_service.publisher.publish_album_row(album_id)

    assert outcome.result == "uncertain"
    album = await db.get_album(album_id)
    assert album["status"] == Status.UNCERTAIN
    assert album["uncertain"] == 1

    # An uncertain album must not be published again automatically.
    album_service.publisher.bot.album_error = None
    again = await album_service.publisher.publish_album_row(album_id)
    assert again.result == "refused"
    assert len(album_service.publisher.bot.album_calls) == 1


# --------------------------------------------------------------------------- #
# Which album combinations Telegram permits
# --------------------------------------------------------------------------- #


def part(media_type):
    return {"media_type": media_type, "file_id": "f"}


@pytest.mark.parametrize("types", [
    ["photo", "photo"],
    ["photo", "video"],
    ["document", "document"],
    ["audio", "audio"],
    ["animation", "animation"],
])
def test_permitted_album_combinations(types):
    validate_album_items([part(t) for t in types])


@pytest.mark.parametrize("types", [
    ["photo", "document"],
    ["video", "audio"],
    ["document", "audio"],
])
def test_forbidden_album_mixtures_are_rejected(types):
    with pytest.raises(AlbumValidationError) as excinfo:
        validate_album_items([part(t) for t in types])
    assert "separate messages" in str(excinfo.value)


def test_album_media_uses_concrete_document_class():
    media = build_album_media([part("document"), part("document")])
    from telegram import InputMediaDocument

    assert all(isinstance(m, InputMediaDocument) for m in media)


def test_album_never_builds_abstract_inputmedia():
    from telegram import InputMedia

    combos = [
        [part("photo"), part("video")],
        [part("document"), part("document")],
        [part("audio"), part("audio")],
        [part("animation"), part("animation")],
    ]
    for combo in combos:
        for media in build_album_media(combo):
            assert type(media) is not InputMedia, "abstract InputMedia must never be built"


def test_album_size_bounds():
    with pytest.raises(AlbumValidationError):
        validate_album_items([part("photo")])
    with pytest.raises(AlbumValidationError):
        validate_album_items([part("photo")] * 11)
    validate_album_items([part("photo")] * 10)


# --------------------------------------------------------------------------- #
# Routing: a document carrying a media_group_id goes to the album collector
# --------------------------------------------------------------------------- #


async def test_document_album_update_routes_to_albums(db, publisher, album_service,
                                                      auto_config, fake_bot):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    for message_id in (1, 2):
        update = attach_fake_bot(
            build_document_update(
                message_id=message_id,
                media_group_id=f"grp-{message_id}" if message_id == 1 else "grp-1",
                caption="Suits EP 1" if message_id == 1 else None,
                thread_id=WEB_SERIES_TOPIC,
            ),
            fake_bot,
        )
        await handlers.handle_media(update, FakeContext())

    collecting = await db.get_albums_by_status(Status.COLLECTING)
    assert len(collecting) == 1, "album parts must not create single-media rows"
    items = await db.get_album_items(collecting[0]["id"])
    assert len(items) == 2
    assert await db.get_item_by_source(SOURCE_CHAT, 1) is None