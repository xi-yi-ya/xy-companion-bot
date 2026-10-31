import uuid
from datetime import UTC, datetime, timedelta

import pytest

from plugin.embeddings import LocalEmbedding
from plugin.memory import TZ, level


async def seed(
    store,
    content="我今天去了公园",
    kind="ordinary",
    at=None,
    account="owner",
    conversation="web:one",
):
    message = await store.record(
        "webchat", account, conversation, uuid.uuid4().hex, "user", content, at
    )

    async def extract(rows, known):
        return [
            {
                "action": "create",
                "content": content,
                "kind": kind,
                "message_id": message["id"],
                "quote": content,
            }
        ]

    result = await store.consolidate(
        message["person_id"], message["created_at"].astimezone(TZ).date(), extract
    )
    return message, result["changed_ids"][0]


def test_decay_boundaries():
    now = datetime.now(UTC)
    for hours, expected in [(0, 10), (47, 10), (48, 9), (96, 8), (144, 7), (191, 7), (192, 6)]:
        assert level(now - timedelta(hours=hours), now) == expected


async def test_daily_archive_and_core_exemption(store):
    old = datetime.now(UTC) - timedelta(days=9)
    msg, mid = await seed(store, at=old)
    _, core = await seed(store, "我一直喜欢猫", "core", old)
    items = {m["id"]: m for m in await store.memories(msg["person_id"])}
    assert items[mid]["status"] == "archived"
    assert items[core]["status"] == "active"
    assert items[mid]["last_user_mentioned_at"] == old


async def test_random_retrieval_does_not_refresh_and_user_reactivates(store):
    msg, mid = await seed(store, at=datetime.now(UTC) - timedelta(days=9))
    current = await store.record(
        "webchat", "owner", "web:one", "next", "user", "还记得我去了公园吗"
    )
    block, _ = await store.context(current["person_id"], current)
    assert str(mid) in block
    await store.search(current["person_id"], "公园")
    memory = (await store.memories(current["person_id"]))[0]
    assert memory["status"] == "archived"
    assert memory["last_user_mentioned_at"] == msg["created_at"]
    await store.confirm(current["person_id"], mid, current["id"], "我去了公园")
    memory = (await store.memories(current["person_id"]))[0]
    assert memory["status"] == "active"
    assert level(memory["last_user_mentioned_at"], datetime.now(UTC)) == 10


async def test_scope_and_quote_validation(store):
    message, mid = await seed(store)
    other = await store.record("webchat", "another", "web:other", "m1", "user", "我今天去了公园")
    with pytest.raises(ValueError):
        await store.confirm(other["person_id"], mid, other["id"], "公园")
    with pytest.raises(ValueError):
        await store.confirm(message["person_id"], mid, message["id"], "虚构原话")
    assistant = await store.record(
        "webchat", "owner", "web:one", "a1", "assistant", "我今天去了公园"
    )
    with pytest.raises(ValueError):
        await store.confirm(message["person_id"], mid, assistant["id"], "公园")


async def test_message_and_daily_job_idempotence(store):
    msg, _ = await seed(store)
    duplicate = await store.record(
        "webchat", "owner", "web:one", msg["event_id"], "user", msg["content"]
    )
    assert duplicate["id"] == msg["id"]

    async def forbidden(*args):
        raise AssertionError("Completed input must not be resummarized")

    result = await store.consolidate(
        msg["person_id"], msg["created_at"].astimezone(TZ).date(), forbidden
    )
    assert result["status"] == "up_to_date"
    assert len(await store.memories(msg["person_id"])) == 1


async def test_invalid_extraction_is_atomic_and_retries_bounded(store):
    msg = await store.record("webchat", "owner", "web:one", "m1", "user", "我喜欢猫")

    async def bad(rows, known):
        return [
            {
                "action": "create",
                "content": "喜欢猫",
                "kind": "core",
                "message_id": msg["id"],
                "quote": "我喜欢猫",
            },
            {
                "action": "create",
                "content": "喜欢狗",
                "kind": "core",
                "message_id": msg["id"],
                "quote": "不存在的引文",
            },
        ]

    for _ in range(3):
        with pytest.raises(ValueError):
            await store.consolidate(msg["person_id"], msg["created_at"].astimezone(TZ).date(), bad)
    assert not await store.memories(msg["person_id"])
    result = await store.consolidate(msg["person_id"], msg["created_at"].astimezone(TZ).date(), bad)
    assert result["status"] == "retry_limit"


async def test_correction_preserves_old_sources(store):
    old, mid = await seed(store, "我最喜欢咖啡", "core")
    msg = await store.record(
        "webchat", "owner", "web:one", "change", "user", "我现在最喜欢茶，不喜欢咖啡了"
    )

    async def correction(rows, known):
        return [
            {
                "action": "replace",
                "target_id": mid,
                "kind": "core",
                "content": "用户现在最喜欢茶",
                "message_id": msg["id"],
                "quote": msg["content"],
            }
        ]

    await store.consolidate(msg["person_id"], msg["created_at"].astimezone(TZ).date(), correction)
    items = {m["id"]: m for m in await store.memories(msg["person_id"], True)}
    assert items[mid]["status"] == "invalid"
    assert items[mid]["sources"][0]["message_id"] == old["id"]
    assert items[mid]["superseded_by"] in items


async def test_budget_and_catchup(store):
    msg, _ = await seed(store, at=datetime.now(UTC) - timedelta(days=2))
    extra = await store.record(
        "webchat",
        "owner",
        "web:one",
        "late",
        "user",
        "我明天去图书馆",
        datetime.now(UTC) - timedelta(days=1),
    )
    pending = await store.pending_days()
    assert any(p["day"] == extra["created_at"].astimezone(TZ).date() for p in pending)
    current = await store.record("webchat", "owner", "web:one", "today", "user", "聊聊公园")
    await store.context(current["person_id"], current)
    async with store.pool.connection() as conn:
        run = await (
            await conn.execute("SELECT * FROM context_runs ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert run["estimated_memory_tokens"] <= 2000
    assert run["estimated_history_tokens"] <= 4000


async def test_real_vector_semantics_and_archived_lookup(store):
    store.embedder = LocalEmbedding("runtime/models/bge-small-zh-v1.5")
    msg, mid = await seed(store, "我最爱吃的水果是苹果", at=datetime.now(UTC) - timedelta(days=9))
    await seed(store, "明天需要修理汽车")
    results = await store.search(msg["person_id"], "我偏爱哪种果子")
    assert results and results[0]["id"] == mid
    assert results[0]["status"] == "archived"
    memory = {m["id"]: m for m in await store.memories(msg["person_id"])}[mid]
    assert memory["last_user_mentioned_at"] == msg["created_at"]
    async with store.pool.connection() as conn:
        dimensions = (
            await (
                await conn.execute(
                    "SELECT public.vector_dims(embedding) AS n FROM memories WHERE id=%s", (mid,)
                )
            ).fetchone()
        )["n"]
    assert dimensions == 512


async def test_persona_settings_persist_and_reject_invalid_changes(store):
    before = await store.get_settings()
    updated = {
        **before,
        "bot_name": "设置测试",
        "persona": "用简短自然的中文聊天。",
        "memory_budget": 2500,
    }
    await store.save_settings(updated)
    assert await store.get_settings() == updated
    for invalid in [
        {**updated, "persona": ""},
        {**updated, "memory_budget": True},
        {**updated, "history_budget": -1},
    ]:
        with pytest.raises(ValueError):
            await store.save_settings(invalid)
    assert await store.get_settings() == updated
