from pathlib import Path

import pytest
from test_qq_chat import Event

from plugin.message_sources import event_source, message_source


async def test_group_provenance_keeps_real_sender_and_room_memory(store):
    event = Event(sender="123456", group="987654")
    user = await store.record(
        "qqgroup:companion_qq:987654",
        "room:456789",
        "companion_qq:GroupMessage:scan_456789_987654",
        "one",
        "user",
        "hello",
        source=event_source(event, "user"),
    )
    reply = await store.record(
        "qqgroup:companion_qq:987654",
        "room:456789",
        user["conversation_id"],
        "one",
        "assistant",
        "hi",
        source=event_source(event, "assistant"),
    )
    assert user["person_id"] == reply["person_id"] == "qqgroup:companion_qq:987654:room:456789"
    assert (user["platform"], user["chat_type"], user["group_id"], user["sender_id"]) == (
        "aiocqhttp",
        "group",
        "987654",
        "123456",
    )
    assert reply["sender_id"] == "456"
    async with store.pool.connection() as conn:
        view = await (
            await conn.execute("SELECT * FROM message_context WHERE id=%s", (user["id"],))
        ).fetchone()
    assert view["sender_id"] == "123456"


async def test_shared_person_preserves_distinct_channels(store):
    await store.record("webchat", "owner", "webchat:FriendMessage:owner", "web", "user", "a")
    async with store.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO platform_accounts VALUES('aiocqhttp','123456','webchat:owner')"
        )
    qq = await store.record(
        "aiocqhttp", "123456", "companion_qq:FriendMessage:123456", "qq", "user", "b"
    )
    assert qq["person_id"] == "webchat:owner" and qq["platform"] == "aiocqhttp"
    assert qq["chat_type"] == "private" and qq["group_id"] is None and qq["sender_id"] == "123456"


async def test_legacy_backfill_is_idempotent_and_does_not_guess_private_bot(store):
    group = await store.record(
        "qqgroup:companion_qq:987654",
        "room:456789",
        "companion_qq:GroupMessage:scan_456789_987654",
        "old",
        "user",
        "[群成员 某人（QQ 123456） 发言]\n你好",
    )
    private = await store.record(
        "aiocqhttp", "123456", "companion_qq:FriendMessage:123456", "reply", "assistant", "hi"
    )
    async with store.pool.connection() as conn:
        await conn.execute(
            "UPDATE messages SET platform=NULL,platform_id=NULL,chat_type='private',group_id=NULL,sender_id=NULL"
        )
        schema = Path("plugin/schema.sql").read_text("utf-8")
        await conn.execute(schema)
        await conn.execute(schema)
        rows = await (await conn.execute("SELECT * FROM messages ORDER BY id")).fetchall()
    assert (
        rows[0]["id"] == group["id"]
        and rows[0]["sender_id"] == "123456"
        and rows[0]["group_id"] == "987654"
    )
    assert rows[1]["id"] == private["id"] and rows[1]["sender_id"] is None


def test_unknown_history_and_invalid_private_group():
    assert (
        message_source("qqgroup:instance:123456", "room:456789", "raw", "user", "unknown")[
            "sender_id"
        ]
        is None
    )
    with pytest.raises(ValueError):
        message_source("webchat", "owner", "raw", "user", source={"group_id": "123456"})
