"""Edit Episode conversation (Phase I / N item 28)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ConversationHandler

import ui
from database import Status
from tests.conftest import ADMIN_ID, SOURCE_CHAT
from tests.test_database import make_album, make_item
from tests.test_handlers import FakeContext, FakeQuery, make_update


def entry_update(kind=ui.KIND_ITEM, row_id=1, user_id=ADMIN_ID):
    query = FakeQuery(ui.encode_callback(ui.ACT_EDIT, kind, row_id), user_id=user_id)
    return SimpleNamespace(callback_query=query, effective_user=query.from_user,
                           effective_chat=None, effective_message=None), query


def text_update(text, user_id=ADMIN_ID):
    return make_update(text=text, user_id=user_id)


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


async def test_edit_available_for_pending_item(handlers, db):
    _, row_id = await make_item(db)
    context = FakeContext()
    update, query = entry_update(row_id=row_id)
    await handlers.cb_edit_entry(update, context)
    assert context.user_data["edit_row_id"] == row_id
    assert context.user_data["edit_kind"] == ui.KIND_ITEM
    assert context.user_data["edit_admin_id"] == ADMIN_ID
    assert "edit_started_at" in context.user_data


async def test_edit_available_for_failed_item(handlers, db):
    _, row_id = await make_item(db)
    await db.claim_item_for_publishing(row_id)
    await db.finish_item_failed(row_id, "boom")
    context = FakeContext()
    update, query = entry_update(row_id=row_id)
    await handlers.cb_edit_entry(update, context)
    assert context.user_data["edit_row_id"] == row_id


@pytest.mark.parametrize("prepare", ["published", "canceled", "uncertain", "publishing"])
async def test_edit_rejected_for_invalid_states(handlers, db, prepare):
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

    context = FakeContext()
    update, query = entry_update(row_id=row_id)
    state = await handlers.cb_edit_entry(update, context)
    assert state == ConversationHandler.END
    assert "cannot edit" in query.answers[0][0].lower()
    assert "edit_row_id" not in context.user_data


async def test_edit_rejected_for_non_admin(handlers, db):
    _, row_id = await make_item(db)
    context = FakeContext()
    update, query = entry_update(row_id=row_id, user_id=999)
    state = await handlers.cb_edit_entry(update, context)
    assert state == ConversationHandler.END
    assert "not authorized" in query.answers[0][0].lower()


async def test_edit_rejected_for_missing_record(handlers):
    context = FakeContext()
    update, query = entry_update(row_id=999999)
    state = await handlers.cb_edit_entry(update, context)
    assert state == ConversationHandler.END
    assert "not found" in query.answers[0][0].lower()


async def test_edit_entry_without_callback_query(handlers):
    update = SimpleNamespace(callback_query=None, effective_user=None, effective_message=None)
    assert await handlers.cb_edit_entry(update, FakeContext()) == ConversationHandler.END


async def test_edit_prompts_for_new_value(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    update, query = entry_update(row_id=row_id)
    await handlers.cb_edit_entry(update, FakeContext())
    text = " ".join(query.message.texts)
    assert "send" in text.lower()
    assert "/cancel" in text


# --------------------------------------------------------------------------- #
# Applying a new episode
# --------------------------------------------------------------------------- #


async def _enter(handlers, row_id, kind=ui.KIND_ITEM, user_id=ADMIN_ID):
    context = FakeContext()
    update, query = entry_update(kind=kind, row_id=row_id, user_id=user_id)
    await handlers.cb_edit_entry(update, context)
    return context, query


async def test_valid_episode_is_persisted(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    update = text_update("1165")
    state = await handlers.on_episode_input(update, context)
    assert state == ConversationHandler.END
    row = await db.get_item(row_id)
    assert row["episode_number"] == "1165"


async def test_edit_regenerates_and_stores_caption(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("42"), context)
    row = await db.get_item(row_id)
    assert "Episode: 42" in row["final_caption"]
    assert "One Piece" in row["final_caption"]


async def test_edit_normalizes_leading_zeros(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("00125"), context)
    assert (await db.get_item(row_id))["episode_number"] == "125"


async def test_edit_accepts_decimal(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("12.5"), context)
    assert (await db.get_item(row_id))["episode_number"] == "12.5"


async def test_edit_accepts_ep_prefixed_value(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("EP 777"), context)
    assert (await db.get_item(row_id))["episode_number"] == "777"


async def test_edit_clears_conversation_state(handlers, db):
    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("1165"), context)
    assert "edit_row_id" not in context.user_data
    assert "edit_kind" not in context.user_data


async def test_edit_updates_the_preview_message(handlers, db, fake_bot):
    _, row_id = await make_item(db, episode_number=None)
    await db.set_item_preview(row_id, 7777)
    context, _ = await _enter(handlers, row_id)
    await handlers.on_episode_input(text_update("2000"), context)
    assert fake_bot.edit_calls
    call = fake_bot.edit_calls[-1]
    assert call["message_id"] == 7777
    assert call["chat_id"] == SOURCE_CHAT
    assert "2000" in call["text"]


# --------------------------------------------------------------------------- #
# Rejected input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "1.000001", "12.5 and more", "EP 12 blah"])
async def test_invalid_input_keeps_session_open(handlers, db, value):
    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(handlers, row_id)
    update = text_update(value)
    state = await handlers.on_episode_input(update, context)
    assert state != ConversationHandler.END
    assert (await db.get_item(row_id))["episode_number"] == "100"
    assert update.message.reply.texts


async def test_invalid_input_does_not_leak_traceback(handlers, db):
    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(handlers, row_id)
    update = text_update("nonsense")
    await handlers.on_episode_input(update, context)
    text = " ".join(update.message.reply.texts)
    assert "Traceback" not in text
    assert ".py" not in text


# --------------------------------------------------------------------------- #
# Session safety
# --------------------------------------------------------------------------- #


async def test_another_admin_cannot_hijack_the_session(env, tmp_path, db, publisher, fake_bot):
    """A second configured admin cannot take over an active session."""
    from config import AppConfig

    from albums import AlbumService
    from handlers import Handlers

    values = dict(env)
    values["ADMIN_IDS"] = "6589890362,111111111"
    two_admin_config = AppConfig.load(values)
    service = AlbumService(db, publisher, fake_bot, two_admin_config)
    two_admin_handlers = Handlers(db, publisher, service, two_admin_config)

    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(two_admin_handlers, row_id, user_id=ADMIN_ID)
    other = text_update("999", user_id=111111111)
    state = await two_admin_handlers.on_episode_input(other, context)
    assert state != ConversationHandler.END
    assert (await db.get_item(row_id))["episode_number"] == "100"
    assert "another admin" in " ".join(other.message.reply.texts).lower()


async def test_state_is_reread_before_applying(handlers, db):
    """If the record is published mid-session the edit must be refused."""
    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(handlers, row_id)

    await db.claim_item_for_publishing(row_id)
    await db.finish_item_published(
        row_id, target_chat_id="@aniwavebd", target_message_ids=[1], final_caption="c")

    update = text_update("2000")
    state = await handlers.on_episode_input(update, context)
    assert state == ConversationHandler.END
    assert (await db.get_item(row_id))["episode_number"] == "100"


async def test_missing_record_mid_session_ends_cleanly(handlers, db):
    _, row_id = await make_item(db)
    context, _ = await _enter(handlers, row_id)
    context.user_data["edit_row_id"] = 999999
    update = text_update("2000")
    assert await handlers.on_episode_input(update, context) == ConversationHandler.END


async def test_unauthorized_user_cannot_complete_edit(handlers, db):
    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(handlers, row_id)
    update = text_update("2000", user_id=999)
    state = await handlers.on_episode_input(update, context)
    assert state == ConversationHandler.END
    assert (await db.get_item(row_id))["episode_number"] == "100"


# --------------------------------------------------------------------------- #
# Albums
# --------------------------------------------------------------------------- #


async def test_album_episode_can_be_edited(handlers, db):
    album_id, _ = await make_album(db)
    await db.finish_album_collection(album_id, None, "")
    context, _ = await _enter(handlers, album_id, kind=ui.KIND_ALBUM)
    await handlers.on_episode_input(text_update("500"), context)
    album = await db.get_album(album_id)
    assert album["episode_number"] == "500"
    assert "Episode: 500" in album["final_caption"]


async def test_album_edit_rejected_when_collecting(handlers, db):
    album_id, _ = await make_album(db)
    context = FakeContext()
    update, query = entry_update(kind=ui.KIND_ALBUM, row_id=album_id)
    state = await handlers.cb_edit_entry(update, context)
    assert state == ConversationHandler.END
    assert "cannot edit" in query.answers[0][0].lower()


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


async def test_conversation_is_registered_first(handlers):
    registered = handlers.get_handlers()
    assert isinstance(registered[0], ConversationHandler)


async def test_conversation_entry_pattern_matches_edit_callbacks(handlers):
    from telegram import CallbackQuery, Chat, Message, Update, User

    conversation = handlers.get_handlers()[0]
    entry = conversation.entry_points[0]
    user = User(id=ADMIN_ID, first_name="a", is_bot=False)
    chat = Chat(id=SOURCE_CHAT, type=Chat.SUPERGROUP)
    message = Message(message_id=1, date=None, chat=chat, from_user=user)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="x", message=message,
        data=ui.encode_callback(ui.ACT_EDIT, ui.KIND_ITEM, 5),
    )
    update = Update(update_id=1, callback_query=query)
    assert entry.check_update(update) is not False


async def test_conversation_has_cancel_fallback_and_timeout(handlers):
    conversation = handlers.get_handlers()[0]
    assert conversation.fallbacks
    assert conversation.conversation_timeout > 0
    assert conversation.per_user is True


async def test_expired_session_is_rejected_and_cleaned(handlers, db):
    """PTB's conversation_timeout is inert without JobQueue, so we enforce it."""
    import time

    _, row_id = await make_item(db, episode_number="100")
    context, _ = await _enter(handlers, row_id)
    context.user_data["edit_started_at"] = time.monotonic() - 10_000

    update = text_update("2000")
    state = await handlers.on_episode_input(update, context)
    assert state == ConversationHandler.END
    assert (await db.get_item(row_id))["episode_number"] == "100"
    assert "expired" in " ".join(update.message.reply.texts).lower()
    assert "edit_row_id" not in context.user_data


async def test_session_within_timeout_is_accepted(handlers, db):
    import time

    _, row_id = await make_item(db, episode_number=None)
    context, _ = await _enter(handlers, row_id)
    context.user_data["edit_started_at"] = time.monotonic() - 1
    await handlers.on_episode_input(text_update("321"), context)
    assert (await db.get_item(row_id))["episode_number"] == "321"