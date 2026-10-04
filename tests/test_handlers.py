"""Command, media-routing and callback authorization tests (Phase J / N 17-18, 26-30)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ui
from database import Status
from tests.conftest import ADMIN_ID, BLEACH_TOPIC, NARUTO_TOPIC, ONE_PIECE_TOPIC, SOURCE_CHAT
from tests.test_database import make_album, make_item


# --------------------------------------------------------------------------- #
# Update / context stubs
# --------------------------------------------------------------------------- #


class FakeReply:
    """Collects the text the handler sends back to the admin."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markup = None
        self.message_id = 4242


class FakeMessage:
    """Minimal telegram.Message stand-in with an async reply_text."""

    def __init__(self, **kwargs) -> None:
        self.message_id = kwargs.get("message_id", 100)
        self.message_thread_id = kwargs.get("message_thread_id")
        self.text = kwargs.get("text")
        self.caption = kwargs.get("caption")
        self.media_group_id = kwargs.get("media_group_id")
        self.edit_date = kwargs.get("edit_date")
        self.from_user = kwargs.get("from_user")
        self.reply = FakeReply()
        self.video = None
        self.animation = None
        self.audio = None
        self.photo = None
        self.document = None
        for kind in list(kwargs.get("media") or {}):
            if kind == "photo":
                # Telegram exposes photos as a list of sizes, largest last.
                setattr(self, kind, [
                    SimpleNamespace(file_id="photo-small", file_unique_id="photo-u-small"),
                    SimpleNamespace(file_id="photo-large", file_unique_id="photo-u-large"),
                ])
            else:
                setattr(self, kind, SimpleNamespace(file_id=f"{kind}-fid", file_unique_id=f"{kind}-uid"))

    async def reply_text(self, text, **kwargs):
        self.reply.texts.append(text)
        self.reply.markup = kwargs.get("reply_markup")
        return SimpleNamespace(message_id=self.reply.message_id, text=text)


class FakeCallbackMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markup = None

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        return SimpleNamespace(message_id=1, text=text)


class FakeQuery:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.message = FakeCallbackMessage()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.message.texts.append(text)
        self.message.markup = kwargs.get("reply_markup")
        return SimpleNamespace(message_id=1, text=text)


class FakeContext:
    def __init__(self, args=None):
        self.args = list(args or [])
        self.user_data: dict = {}
        self.application = None


def make_update(
    *,
    user_id=ADMIN_ID,
    chat_id=SOURCE_CHAT,
    chat_type="supergroup",
    thread_id=ONE_PIECE_TOPIC,
    text=None,
    caption=None,
    message_id=100,
    media=None,
    media_group_id=None,
    is_bot=False,
    edit_date=None,
    user=None,
):
    sender = user or SimpleNamespace(id=user_id, is_bot=is_bot)
    message = FakeMessage(
        message_id=message_id,
        message_thread_id=thread_id,
        text=text,
        caption=caption,
        media_group_id=media_group_id,
        edit_date=edit_date,
        from_user=sender,
        media=media,
    )
    return SimpleNamespace(
        effective_user=sender,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="AniWave Database"),
        effective_message=message,
        callback_query=None,
        message=message,
    )


# --------------------------------------------------------------------------- #
# Authorization (Phase J.1-4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", [
    "cmd_start", "cmd_help", "cmd_status", "cmd_debug", "cmd_chatid",
    "cmd_topicid", "cmd_retry", "cmd_uncertain", "cmd_cancel",
])
async def test_commands_reject_non_admins(handlers, command):
    update = make_update(user_id=123456789)
    await getattr(handlers, command)(update, FakeContext())
    assert update.message.reply.texts
    assert "not authorized" in update.message.reply.texts[0].lower()


async def test_non_admin_cannot_trigger_publishing(handlers, db):
    update = make_update(user_id=123456789, media={"video": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_unauthorized_callback_is_answered(handlers):
    query = FakeQuery(ui.encode_callback(ui.ACT_PUBLISH, ui.KIND_ITEM, 1), user_id=999)
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                             effective_chat=None, effective_message=None)
    await handlers.handle_callback(update, FakeContext())
    assert query.answers
    assert "not authorized" in query.answers[0][0].lower()


async def test_authorization_uses_numeric_id_not_username(handlers, db):
    """A matching username with a different numeric id is rejected."""
    update = make_update(user_id=999, media={"video": True}, caption="EP 5")
    update.effective_user = SimpleNamespace(id=999, username="admin", is_bot=False)
    update.message.from_user = update.effective_user
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


# --------------------------------------------------------------------------- #
# Missing update fields (Phase J.4 / N item 30)
# --------------------------------------------------------------------------- #


async def test_missing_effective_user_is_safe(handlers):
    update = make_update()
    update.effective_user = None
    await handlers.cmd_status(update, FakeContext())


async def test_missing_effective_chat_is_safe(handlers):
    update = make_update()
    update.effective_chat = None
    await handlers.cmd_chatid(update, FakeContext())


async def test_missing_callback_query_is_safe(handlers):
    update = SimpleNamespace(callback_query=None, effective_user=None, effective_message=None)
    await handlers.handle_callback(update, FakeContext())


async def test_malformed_callback_data_is_safe(handlers):
    query = FakeQuery("garbage")
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                             effective_message=None, effective_chat=None)
    await handlers.handle_callback(update, FakeContext())
    assert "unknown action" in query.answers[0][0].lower()


@pytest.mark.parametrize("data", [
    "aw:p:m", "aw:p:x:1", "aw:zz:m:1", "aw:p:m:abc", "aw:p:m:1;drop", "", None,
])
def test_callback_data_validation_rejects_junk(data):
    assert ui.decode_callback(data) is None


def test_callback_data_roundtrip():
    data = ui.encode_callback(ui.ACT_PUBLISH, ui.KIND_ALBUM, 123)
    assert ui.decode_callback(data) == (ui.ACT_PUBLISH, ui.KIND_ALBUM, 123)


def test_callback_data_is_compact():
    data = ui.encode_callback(ui.ACT_UNCERTAIN_PUBLISHED, ui.KIND_ITEM, 999999999)
    assert len(data.encode()) <= 64


def test_edit_callback_is_not_handled_by_generic_handler():
    """The conversation must see aw:e: before the generic handler."""
    import inspect

    source = inspect.getsource(type(handlers_stub()).get_handlers)
    assert "aw:(?!e:)" in source


def handlers_stub():
    from database import Database
    from handlers import Handlers

    return Handlers.__new__(Handlers)


@pytest.mark.parametrize("reference,expected", [
    ("12", (ui.KIND_ITEM, 12)),
    ("m:12", (ui.KIND_ITEM, 12)),
    ("a:7", (ui.KIND_ALBUM, 7)),
    ("A:7", (ui.KIND_ALBUM, 7)),
])
def test_retry_target_parsing(reference, expected):
    assert ui.parse_target(reference) == expected


@pytest.mark.parametrize("reference", ["", None, "x:1", "abc", "1:2", "m:"])
def test_retry_target_parsing_rejects_junk(reference):
    assert ui.parse_target(reference) is None


# --------------------------------------------------------------------------- #
# Media routing (Required behaviour 1-5)
# --------------------------------------------------------------------------- #


async def test_media_from_other_group_is_ignored(handlers, db):
    update = make_update(chat_id=-1009999999999, media={"video": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_media_from_private_chat_is_ignored(handlers, db):
    update = make_update(chat_type="private", media={"video": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


@pytest.mark.parametrize("thread_id", [999, None])
async def test_media_outside_configured_topic_is_ignored(handlers, db, thread_id):
    update = make_update(thread_id=thread_id, media={"video": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_bot_messages_are_ignored(handlers, db):
    update = make_update(is_bot=True, media={"video": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_edited_messages_are_ignored(handlers, db):
    update = make_update(media={"video": True}, caption="EP 5", edit_date="2026-01-01")
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_text_only_message_is_ignored(handlers, db):
    update = make_update(text="just text", caption=None)
    await handlers.handle_media(update, FakeContext())
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


@pytest.mark.parametrize("topic_id", [ONE_PIECE_TOPIC, NARUTO_TOPIC, BLEACH_TOPIC])
async def test_all_configured_topics_are_accepted(handlers, db, topic_id):
    update = make_update(thread_id=topic_id, media={"photo": True}, caption="EP 5")
    await handlers.handle_media(update, FakeContext())
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row is not None
    assert row["topic_id"] == topic_id


async def test_topic_determines_anime_title(handlers, db):
    await handlers.handle_media(
        make_update(thread_id=NARUTO_TOPIC, media={"video": True}, caption="Naruto EP 25"),
        FakeContext(),
    )
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row["anime_title"] == "Naruto"
    assert row["episode_number"] == "25"


# --------------------------------------------------------------------------- #
# Approval mode (Required behaviour 8)
# --------------------------------------------------------------------------- #


async def test_approval_mode_creates_preview_with_buttons(handlers, db):
    await handlers.handle_media(
        make_update(media={"video": True}, caption="One Piece EP 1165"), FakeContext()
    )
    reply = handlers.update_replies[-1] if hasattr(handlers, "update_replies") else None
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row["status"] == Status.PENDING
    assert row["preview_message_id"] == 4242


async def test_approval_mode_does_not_publish(handlers, db, fake_bot):
    await handlers.handle_media(
        make_update(media={"video": True}, caption="One Piece EP 1165"), FakeContext()
    )
    assert fake_bot.send_video_calls == []


async def test_missing_episode_keeps_pending_and_warns(handlers, db, fake_bot):
    update = make_update(media={"video": True}, caption="no episode here")
    await handlers.handle_media(update, FakeContext())
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row["status"] == Status.PENDING
    assert row["episode_number"] is None
    assert fake_bot.send_video_calls == []
    text = " ".join(update.message.reply.texts)
    assert "Edit Episode" in text or "episode" in text.lower()


async def test_duplicate_media_update_is_a_noop(handlers, db, fake_bot):
    for _ in range(3):
        await handlers.handle_media(
            make_update(media={"video": True}, caption="One Piece EP 1165"), FakeContext()
        )
    counts = await db.counts_by_state("media_items")
    assert counts[Status.PENDING] == 1


# --------------------------------------------------------------------------- #
# Automatic mode (Required behaviour 8)
# --------------------------------------------------------------------------- #


async def test_auto_mode_publishes_immediately(db, publisher, album_service, auto_config):
    from handlers import Handlers

    auto_handlers = Handlers(db, publisher, album_service, auto_config)
    await auto_handlers.handle_media(
        make_update(media={"video": True}, caption="One Piece EP 1165"), FakeContext()
    )
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row["status"] == Status.PUBLISHED


async def test_auto_mode_without_episode_does_not_publish(db, publisher, album_service,
                                                           auto_config, fake_bot):
    from handlers import Handlers

    auto_handlers = Handlers(db, publisher, album_service, auto_config)
    await auto_handlers.handle_media(
        make_update(media={"video": True}, caption="no episode"), FakeContext()
    )
    row = await db.get_item_by_source(SOURCE_CHAT, 100)
    assert row["status"] == Status.PENDING
    assert fake_bot.send_video_calls == []


# --------------------------------------------------------------------------- #
# Album routing (Phase F)
# --------------------------------------------------------------------------- #


async def test_album_parts_are_routed_to_the_album_service(handlers, db):
    for message_id in (10, 11, 12):
        await handlers.handle_media(
            make_update(
                message_id=message_id,
                media={"photo": True},
                media_group_id="grp-1",
                caption="One Piece EP 1165" if message_id == 10 else None,
            ),
            FakeContext(),
        )
    albums = await db.get_albums_by_status(Status.COLLECTING)
    assert len(albums) == 1
    items = await db.get_album_items(albums[0]["id"])
    assert [i["source_message_id"] for i in items] == [10, 11, 12]


async def test_album_parts_do_not_create_single_rows(handlers, db):
    await handlers.handle_media(
        make_update(message_id=10, media={"photo": True}, media_group_id="grp-1",
                    caption="EP 1165"),
        FakeContext(),
    )
    assert await db.get_item_by_source(SOURCE_CHAT, 10) is None


# --------------------------------------------------------------------------- #
# Publish callback (Phase G.3-4)
# --------------------------------------------------------------------------- #


async def _publish_callback(handlers, kind, row_id, user_id=ADMIN_ID):
    query = FakeQuery(ui.encode_callback(ui.ACT_PUBLISH, kind, row_id), user_id=user_id)
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                             effective_chat=None, effective_message=None)
    await handlers.handle_callback(update, FakeContext())
    return query


async def test_publish_callback_publishes(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    query = await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED
    assert len(fake_bot.send_video_calls) == 1
    assert "Published successfully" in " ".join(query.message.texts)


async def test_publish_callback_is_idempotent_on_double_press(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    assert len(fake_bot.send_video_calls) == 1


async def test_concurrent_publish_callbacks_send_once(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    queries = [FakeQuery(ui.encode_callback(ui.ACT_PUBLISH, ui.KIND_ITEM, row_id))
               for _ in range(8)]
    await asyncio.gather(*[
        handlers.handle_callback(
            SimpleNamespace(callback_query=q, effective_user=q.from_user,
                            effective_chat=None, effective_message=None),
            FakeContext(),
        )
        for q in queries
    ])
    assert len(fake_bot.send_video_calls) == 1
    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED


@pytest.mark.parametrize("prepare,fragment", [
    ("published", "cannot publish"),
    ("canceled", "cannot publish"),
    ("uncertain", "cannot publish"),
    ("publishing", "cannot publish"),
])
async def test_publish_callback_rejects_invalid_states(handlers, db, fake_bot, prepare, fragment):
    _, row_id = await make_item(db)
    if prepare == Status.PUBLISHED:
        await db.claim_item_for_publishing(row_id)
        await db.finish_item_published(
            row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption="c")
    elif prepare == Status.CANCELED:
        await db.cancel_item(row_id)
    elif prepare == Status.UNCERTAIN:
        await db.claim_item_for_publishing(row_id)
        await db.mark_item_uncertain(row_id, "crash")
    else:
        await db.claim_item_for_publishing(row_id)
    calls_before = len(fake_bot.send_video_calls)
    query = await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    assert len(fake_bot.send_video_calls) == calls_before
    assert any(fragment in text.lower() for text, _ in query.answers)


async def test_publish_callback_for_missing_record(handlers):
    query = await _publish_callback(handlers, ui.KIND_ITEM, 999999)
    assert "not found" in query.answers[0][0].lower()


async def test_album_publish_callback(handlers, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo", file_id="f")
    await db.finish_album_collection(album_id, "1165", "cap")
    query = await _publish_callback(handlers, ui.KIND_ALBUM, album_id)
    assert (await db.get_album(album_id))["status"] == Status.PUBLISHED
    assert len(fake_bot.album_calls) == 1


async def test_uncertain_outcome_offers_resolution_buttons(handlers, db, fake_bot):
    from telegram.error import TimedOut

    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")

    fake_bot.video_error = TimedOut()
    _, retry_row = await make_item(db, source_message_id=200)
    query = await _publish_callback(handlers, ui.KIND_ITEM, retry_row)
    assert (await db.get_item(retry_row))["status"] == Status.UNCERTAIN
    text = " ".join(query.message.texts)
    assert "UNKNOWN" in text or "unknown" in text
    assert query.message.markup is not None


# --------------------------------------------------------------------------- #
# Cancel callback (Phase G.5)
# --------------------------------------------------------------------------- #


async def test_cancel_callback_cancels_pending(handlers, db):
    _, row_id = await make_item(db)
    query = FakeQuery(ui.encode_callback(ui.ACT_CANCEL, ui.KIND_ITEM, row_id))
    await handlers.handle_callback(
        SimpleNamespace(callback_query=query, effective_user=query.from_user,
                        effective_chat=None, effective_message=None),
        FakeContext(),
    )
    assert (await db.get_item(row_id))["status"] == Status.CANCELED


async def test_cancel_callback_rejects_published(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption="c")
    query = FakeQuery(ui.encode_callback(ui.ACT_CANCEL, ui.KIND_ITEM, row_id))
    await handlers.handle_callback(
        SimpleNamespace(callback_query=query, effective_user=query.from_user,
                        effective_chat=None, effective_message=None),
        FakeContext(),
    )
    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED
    assert "cannot cancel" in query.answers[0][0].lower()


async def test_cancel_album_callback(handlers, db):
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "5", "cap")
    query = FakeQuery(ui.encode_callback(ui.ACT_CANCEL, ui.KIND_ALBUM, album_id))
    await handlers.handle_callback(
        SimpleNamespace(callback_query=query, effective_user=query.from_user,
                        effective_chat=None, effective_message=None),
        FakeContext(),
    )
    assert (await db.get_album(album_id))["status"] == Status.CANCELED


async def test_canceled_item_cannot_be_published(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.cancel_item(row_id)
    await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    assert fake_bot.send_video_calls == []
    assert (await db.get_item(row_id))["status"] == Status.CANCELED


# --------------------------------------------------------------------------- #
# Uncertain resolution (Phase H.6-7)
# --------------------------------------------------------------------------- #


async def _uncertain_callback(handlers, action, kind, row_id):
    query = FakeQuery(ui.encode_callback(action, kind, row_id))
    await handlers.handle_callback(
        SimpleNamespace(callback_query=query, effective_user=query.from_user,
                        effective_chat=None, effective_message=None),
        FakeContext(),
    )
    return query


async def test_uncertain_can_be_marked_published(handlers, db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    await _uncertain_callback(handlers, ui.ACT_UNCERTAIN_PUBLISHED, ui.KIND_ITEM, row_id)
    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED


async def test_uncertain_absent_requires_second_confirmation(handlers, db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")

    query = await _uncertain_callback(handlers, ui.ACT_UNCERTAIN_ABSENT, ui.KIND_ITEM, row_id)
    assert (await db.get_item(row_id))["status"] == Status.UNCERTAIN
    text = " ".join(query.message.texts)
    assert "sure" in text.lower() or "duplicate" in text.lower()
    assert query.message.markup is not None


async def test_uncertain_confirm_absent_makes_retryable(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    await _uncertain_callback(handlers, ui.ACT_UNCERTAIN_ABSENT, ui.KIND_ITEM, row_id)
    await _uncertain_callback(handlers, ui.ACT_CONFIRM_ABSENT, ui.KIND_ITEM, row_id)
    assert (await db.get_item(row_id))["status"] == Status.FAILED

    await _publish_callback(handlers, ui.KIND_ITEM, row_id)
    assert len(fake_bot.send_video_calls) == 1


async def test_uncertain_confirmation_rejected_from_wrong_state(handlers, db):
    _, row_id = await make_item(db)
    query = await _uncertain_callback(handlers, ui.ACT_UNCERTAIN_PUBLISHED, ui.KIND_ITEM, row_id)
    assert "no longer uncertain" in query.answers[0][0].lower()


async def test_uncertain_album_resolution(handlers, db):
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, "5", "cap")
    await db.claim_album_for_publishing(album_id)
    await db.mark_album_uncertain(album_id, "crash")
    await _uncertain_callback(handlers, ui.ACT_CONFIRM_ABSENT, ui.KIND_ALBUM, album_id)
    assert (await db.get_album(album_id))["status"] == Status.PENDING


# --------------------------------------------------------------------------- #
# /retry rules (Phase H.8-9)
# --------------------------------------------------------------------------- #


async def test_retry_without_arguments_shows_usage(handlers):
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext())
    assert "Usage" in update.message.reply.texts[0]


async def test_retry_with_bad_argument(handlers):
    update = make_update(text="/retry nope")
    await handlers.cmd_retry(update, FakeContext(["nope"]))
    assert "Could not read" in update.message.reply.texts[0]


async def test_retry_missing_record(handlers):
    update = make_update(text="/retry 999")
    await handlers.cmd_retry(update, FakeContext(["999"]))
    assert "No m:999" in update.message.reply.texts[0]


async def test_retry_publishes_failed_item(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_failed(row_id, "rate limited")

    update = make_update(text="/retry 1")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED
    assert len(fake_bot.send_video_calls) == 1


async def test_retry_refuses_published(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption="c")
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert "already published" in update.message.reply.texts[0]
    assert fake_bot.send_video_calls == []


async def test_retry_refuses_uncertain(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert "uncertain" in update.message.reply.texts[0]
    assert fake_bot.send_video_calls == []


async def test_retry_refuses_canceled(handlers, db):
    _, row_id = await make_item(db)
    await db.cancel_item(row_id)
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert "canceled" in update.message.reply.texts[0]


async def test_retry_refuses_publishing(handlers, db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert "already publishing" in update.message.reply.texts[0]


async def test_retry_refuses_pending_and_points_at_button(handlers, db, fake_bot):
    _, row_id = await make_item(db)
    update = make_update(text="/retry")
    await handlers.cmd_retry(update, FakeContext([str(row_id)]))
    assert "Publish" in update.message.reply.texts[0]
    assert fake_bot.send_video_calls == []


async def test_retry_album_by_kind(handlers, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo", file_id="f")
    await db.finish_album_collection(album_id, "5", "cap")
    await db.claim_album_for_publishing(album_id)
    await db.finish_album_failed(album_id, "boom")

    update = make_update(text="/retry a:1")
    await handlers.cmd_retry(update, FakeContext([f"a:{album_id}"]))
    assert (await db.get_album(album_id))["status"] == Status.PUBLISHED


# --------------------------------------------------------------------------- #
# Informational commands (Required behaviour 12)
# --------------------------------------------------------------------------- #


async def test_status_reports_configuration(handlers, db):
    await make_item(db)
    update = make_update(text="/status")
    await handlers.cmd_status(update, FakeContext())
    text = update.message.reply.texts[0]
    assert str(SOURCE_CHAT) in text
    assert "@aniwavebd" in text
    assert "approval" in text
    assert "Database: OK" in text
    assert "One Piece" in text


async def test_debug_reports_safe_diagnostics_only(handlers, config):
    update = make_update(text="/debug", media={"video": True})
    await handlers.cmd_debug(update, FakeContext())
    text = update.message.reply.texts[0]
    assert str(SOURCE_CHAT) in text
    assert str(ONE_PIECE_TOPIC) in text
    assert str(ADMIN_ID) in text
    # Must not leak secrets or local paths.
    assert config.bot_token not in text
    assert "Traceback" not in text
    assert str(config.database_path) not in text or config.database_path in text
    for secret in config.secrets():
        assert secret not in text


async def test_debug_includes_media_details(handlers):
    update = make_update(text="/debug", media={"photo": True})
    await handlers.cmd_debug(update, FakeContext())
    text = update.message.reply.texts[0]
    assert "photo" in text
    assert "Media group id" in text


async def test_chatid_and_topicid(handlers):
    update = make_update(text="/chatid")
    await handlers.cmd_chatid(update, FakeContext())
    assert str(SOURCE_CHAT) in update.message.reply.texts[0]

    update2 = make_update(text="/topicid")
    await handlers.cmd_topicid(update2, FakeContext())
    assert str(ONE_PIECE_TOPIC) in update2.message.reply.texts[0]


async def test_uncertain_command_lists_records(handlers, db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.mark_item_uncertain(row_id, "crash")
    update = make_update(text="/uncertain")
    await handlers.cmd_uncertain(update, FakeContext())
    text = update.message.reply.texts[0]
    assert str(row_id) in text
    assert "retried automatically" in text.lower()


async def test_uncertain_command_with_nothing_to_review(handlers):
    update = make_update(text="/uncertain")
    await handlers.cmd_uncertain(update, FakeContext())
    assert "No interrupted" in update.message.reply.texts[0]


async def test_help_and_start_render(handlers):
    for command in ("cmd_help", "cmd_start"):
        update = make_update(text="/x")
        await getattr(handlers, command)(update, FakeContext())
        text = update.message.reply.texts[0]
        assert "/status" in text
        assert "/topicid" in text


async def test_status_output_contains_no_markdown(handlers, db):
    """Status is plain text: no parse mode, no markdown control characters."""
    await make_item(db)
    update = make_update(text="/status")
    await handlers.cmd_status(update, FakeContext())
    text = update.message.reply.texts[0]
    assert update.message.reply.markup is None
    assert "**" not in text
    assert "```" not in text