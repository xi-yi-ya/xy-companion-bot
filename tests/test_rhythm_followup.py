import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.followup import DEFAULT_FOLLOWUP, local_reason, validate_followup
from plugin.proactive import DEFAULT_CONTACT, OWNER, ContactService
from plugin.rhythm import DEFAULT_RHYTHM, Rhythm, bubbles, delay, validate_rhythm

NOW = datetime(2026, 11, 2, 4, tzinfo=UTC)
TARGET = {"umo": "companion_qq:FriendMessage:123", "account": "123", "self_id": "456"}
TEXT = "这是一个完整的自然段，我们刚才讨论的想法让我又想到一些具体细节。\n\n第二个想法可以独立说明，但不需要为了表现自然刻意切成很多碎片。\n\n第三段保留结尾。"


@pytest.mark.parametrize(
    "text",
    [
        "先看代码\n\n```python\na = 1\n\nprint(a)\n```\n\n说明",
        "参考 https://example.com/a?q=a.b\n\n详细说明",
        "> 引用\n> 第二行\n\n解释",
        "1. 列表\n\n2. 后续",
        "|a|b|\n|-|-|\n\n正文",
        "我说“别着急。再想想。”\n\n另一段",
        "内联 `a.b()`\n\n说明",
        "[链接](https://example.com)\n\n之后",
    ],
)
def test_structures_preserved(text):
    assert bubbles(text) == [text]


def test_natural_bubbles_bounded_not_character_cut():
    assert len(bubbles(TEXT)) == 3
    assert len(bubbles(TEXT * 8)) <= 3
    assert bubbles("你好") == ["你好"]
    assert bubbles("很长但是没有句子分隔符" * 80) == ["很长但是没有句子分隔符" * 80]
    assert bubbles(TEXT, "single") == [TEXT]
    assert bubbles("两个句子。另一个意思。", "paragraph") == ["两个句子。另一个意思。"]


def test_elapsed_inference_time_not_added_twice():
    assert delay(TEXT, DEFAULT_RHYTHM, elapsed=30) == 0
    assert 1.5 <= delay(TEXT, DEFAULT_RHYTHM) <= 5
    assert delay(TEXT, DEFAULT_RHYTHM, first=False) <= 2.5
    assert delay(TEXT, {**DEFAULT_RHYTHM, "enabled": False}) == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"min_seconds": -1},
        {"max_seconds": float("nan")},
        {"enabled": 1},
        {"strategy": "chars"},
        {"min_seconds": 7},
    ],
)
def test_rhythm_validation(patch):
    with pytest.raises(ValueError):
        validate_rhythm({**DEFAULT_RHYTHM, **patch})


class Clock:
    value = 0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds
        await asyncio.sleep(0)


async def test_cancel_mid_bubbles_only_delivered_history_and_no_replay(store):
    clock = Clock()
    rhythm = Rhythm(store.pool, clock, clock.sleep)
    valid, sent, history = True, [], []

    async def current():
        return valid

    async def send(text, index):
        nonlocal valid
        sent.append(text)
        valid = False
        return {"status": "sent"}

    async def record(text, index):
        history.append(text)

    result = await rhythm.deliver("owner", TEXT, 0, current, send, record, source_id=101)
    assert result["status"] == "cancelled" and len(sent) == 1 and sent == history
    valid = True
    result = await rhythm.deliver("owner", TEXT, 0, current, send, record, source_id=101)
    assert result["status"] == "duplicate" and len(sent) == 1
    assert not rhythm.locks


async def test_shared_lane_serializes_concurrent_tasks(store):
    clock = Clock()
    rhythm = Rhythm(store.pool, clock, clock.sleep)
    output = []

    async def current():
        return True

    async def record(*args):
        pass

    def sender(label):
        async def send(text, index):
            output.append(label)
            await asyncio.sleep(0)
            return {"status": "sent"}

        return send

    await asyncio.gather(
        *(rhythm.deliver("same-room", TEXT, 0, current, sender(n), record) for n in (1, 2))
    )
    assert output == [1, 1, 1, 2, 2, 2]


async def test_unknown_delivery_stops_and_restart_does_not_replay(store):
    rhythm = Rhythm(store.pool)
    await rhythm.save({**DEFAULT_RHYTHM, "min_seconds": 0, "max_seconds": 0})
    sent = []

    async def current():
        return True

    async def record(*args):
        raise AssertionError("unknown must not enter history")

    async def send(text, index):
        sent.append(index)
        return {"status": "unknown"}

    assert (await rhythm.deliver("key", TEXT, 0, current, send, record, source_id=11))[
        "status"
    ] == "unknown"
    async with store.pool.connection() as conn:
        await conn.execute("INSERT INTO qq_reply_runs(source_id,status) VALUES(12,'running')")
    await rhythm.recover()
    assert (await rhythm.deliver("key", TEXT, 0, current, send, record, source_id=12))[
        "status"
    ] == "duplicate"
    assert sent == [0]


async def test_new_input_during_first_wait_sends_nothing(store):
    clock = Clock()
    rhythm = Rhythm(store.pool, clock, clock.sleep)

    async def current():
        return clock.value < 0.2

    async def forbidden(*args):
        raise AssertionError("Cancelled reply must not send or record")

    result = await rhythm.deliver("key", TEXT, 0, current, forbidden, forbidden)
    assert result["status"] == "cancelled" and not result["sent"]


@pytest.mark.parametrize("group", ["", "12345"])
@pytest.mark.parametrize("is_current", [True, False])
async def test_final_qq_hook_preserves_quote_and_at_only_first_bubble(
    store, tmp_path, group, is_current
):
    # Run the actual production hook with the real PG store + QQAgent audit.
    # Avoid importing/starting a second AstrBot process in the unit test runner.
    import time

    from test_qq_chat import Event

    from plugin.message_sources import event_source
    from plugin.qq_agent import QQAgent, group_permission
    from plugin.qq_chat import delivery_key, record_account, record_platform
    from plugin.qq_policy import quote_target

    class Plain:
        def __init__(self, text):
            self.text = text

    class Reply:
        def __init__(self, id):
            self.id = id

    class At:
        def __init__(self, qq):
            self.qq = qq

    class Image:
        pass

    tree = ast.parse(Path("plugin/main.py").read_text("utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    method = next(
        n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "quote_qq_reply"
    )
    method.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            method,
        ],
        type_ignores=[],
    )
    ns = {
        **globals(),
        **locals(),
        "event_source": event_source,
        "group_permission": group_permission,
        "delivery_key": delivery_key,
        "record_account": record_account,
        "record_platform": record_platform,
        "quote_target": quote_target,
        "time": time,
    }
    exec(compile(ast.fix_missing_locations(module), "plugin/main.py", "exec"), ns)
    source = await setup(store)
    identity = {"online": True, "bot_qq": "456", "owner_qq": "123"}
    wire = []

    async def call(action, **args):
        wire.append(args["message"])
        return {"message_id": 100 + len(wire)}

    agent = QQAgent(store.pool, call, lambda: identity, tmp_path)
    if group:
        settings = await agent.settings()
        settings["group_rules"] = [{"group_id": group, "read": True, "reply": True, "media": False}]
        await agent.save(settings)
    rhythm = Rhythm(store.pool)
    await rhythm.save({**DEFAULT_RHYTHM, "min_seconds": 0, "max_seconds": 0})
    event = Event(group=group)
    event.extra.update(
        companion_reply=TEXT,
        companion_message=source,
        companion_quote_target={"id": "789", "sender": "123"},
    )
    result = SimpleNamespace(chain=[Plain(TEXT)])
    event.get_result = lambda: result
    event.clear_result = lambda: setattr(event, "get_result", lambda: None)
    obj = SimpleNamespace(
        qq_agent=agent,
        qq=SimpleNamespace(snapshot=lambda: identity),
        store=store,
        rhythm=rhythm,
        qq_ingress=SimpleNamespace(current=lambda e: is_current),
    )
    await ns["quote_qq_reply"](obj, event)
    if not is_current:
        assert not wire and event.stopped and event.get_result() is None
        return
    assert event.stopped and event.extra["companion_rhythm_handled"]
    assert event.get_result() is None, "AstrBot respond stage must not send the full result again"
    assert len(wire) == 3 and wire[0][0] == {"type": "reply", "data": {"id": "789"}}
    if group:
        assert wire[0][1] == {"type": "at", "data": {"qq": "123"}}
    assert all(p[0]["type"] == "text" for p in wire[1:])
    async with store.pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT content FROM messages WHERE role='assistant' ORDER BY id")
        ).fetchall()
    assert [r["content"] for r in rows] == bubbles(TEXT)
    # Re-entering the hook cannot dispatch a second reply for this source.
    await ns["quote_qq_reply"](obj, event)
    assert len(wire) == 3


class EligibleRandom:
    def randint(self, lo, hi):
        return lo if lo == 0 else (lo + hi) // 2


async def setup(store):
    store.contact.rng = EligibleRandom()
    await store.contact.save({**DEFAULT_CONTACT, "quiet_enabled": False}, NOW)
    await store.contact.save_followup(DEFAULT_FOLLOWUP)
    async with store.pool.connection() as conn:
        await conn.execute("INSERT INTO persons(id) VALUES(%s) ON CONFLICT DO NOTHING", (OWNER,))
        await conn.execute(
            "INSERT INTO platform_accounts(platform,account_id,person_id) VALUES('aiocqhttp','123',%s)",
            (OWNER,),
        )
    source = await store.record(
        "aiocqhttp",
        "123",
        TARGET["umo"],
        uuid.uuid4().hex,
        "user",
        "我们刚才聊到小说里的人物关系，这里还有很多值得慢慢想的细节",
        NOW,
    )
    await store.contact.note_reply(source, TEXT, NOW)
    return source


class Harness:
    def __init__(self, store):
        self.now = NOW + timedelta(minutes=15)
        self.calls, self.sent = 0, 0
        self.online = True
        self.hook = None
        self.result = {
            "send": True,
            "text": "刚才那个角色的选择，我又想到一个角度。",
            "reason": "承接小说人物选择",
        }
        self.service = ContactService(
            store.contact,
            self.generate,
            self.send,
            self.target,
            lambda: False,
            self.current,
            lambda: self.now,
        )

    async def target(self):
        return TARGET if self.online else None

    async def current(self, result):
        return True

    async def generate(self, run):
        assert run["kind"] == "followup"
        self.calls += 1
        if self.hook:
            await self.hook()
        return self.result

    async def send(self, target, text):
        self.sent += 1
        return {"message_id": 1}


async def test_followup_shares_no_reply_gate_with_long_contact(store):
    await setup(store)
    h = Harness(store)
    await h.service.step()
    assert h.calls == h.sent == 1
    h.now += timedelta(hours=24)
    await h.service.step()
    assert h.sent == 1
    snap = await store.contact.snapshot()
    assert snap["state"]["awaiting_reply"] and snap["last_followup"]["status"] == "sent"


async def test_concurrent_claims_and_generation_new_user_cancels(store):
    await setup(store)
    h = Harness(store)

    async def incoming():
        await store.record("webchat", "owner", "test:web", "new", "user", "我回来了", h.now)

    h.hook = incoming
    await asyncio.gather(h.service.step(), h.service.step())
    assert h.calls == 1 and h.sent == 0
    assert (await store.contact.snapshot())["last_followup"]["status"] == "cancelled"


async def test_followup_disabled_during_generation(store):
    await setup(store)
    h = Harness(store)

    async def disable():
        await store.contact.save_followup({**DEFAULT_FOLLOWUP, "enabled": False})

    h.hook = disable
    await h.service.step()
    assert h.sent == 0
    assert (await store.contact.snapshot())["last_followup"]["reason"] == "settings_changed"


async def test_offline_window_expires_without_model_or_late_send(store):
    await setup(store)
    h = Harness(store)
    h.online = False
    await h.service.step()
    h.now += timedelta(minutes=30)
    await h.service.step()
    assert (await store.contact.snapshot())["state"]["followup_reason"] == "window_expired"
    h.online = True
    await h.service.step()
    assert h.calls == h.sent == 0


async def test_skipped_judgment_not_repeated_on_restart(store):
    await setup(store)
    h = Harness(store)
    h.result = {"send": False, "text": "", "reason": "没有有价值的补充"}
    await h.service.step()
    await store.contact.recover(h.now, startup=True)
    await store.contact.seed(h.now)
    await h.service.step()
    assert h.calls == 1 and h.sent == 0


async def test_restart_during_generation_consumes_candidate_only(store):
    await setup(store)
    due_before = (await store.contact.snapshot())["state"]["due_at"]
    run = await store.contact.claim(NOW + timedelta(minutes=15), TARGET)
    assert run["kind"] == "followup"
    await store.contact.recover(NOW + timedelta(minutes=16), startup=True)
    s = (await store.contact.snapshot())["state"]
    assert s["due_at"] == due_before and s["followup_due"] is None and s["current_run"] is None


async def test_dnd_and_closed_topic_no_candidate(store):
    source = await setup(store)
    source = {**source, "id": source["id"] + 1}
    # Pure gate evidence; persisted DND checked on the real source separately.
    assert local_reason("晚安我先睡了", TEXT) == "conversation_closed"
    assert local_reason("还有些话题", "你觉得应该怎么办？") == "waiting_for_answer"
    await store.contact.save({**DEFAULT_CONTACT, "quiet_start": "11:00", "quiet_end": "14:00"}, NOW)
    h = Harness(store)
    await h.service.step()
    assert h.calls == h.sent == 0


async def test_quota_and_random_skip_no_model(store):
    await setup(store)
    async with store.pool.connection() as conn:
        for n in range(4):
            await conn.execute(
                "INSERT INTO proactive_deliveries(id,person_id,source_user_id,target,status,lease_until,kind,created_at) VALUES(%s,%s,%s,'{}','skipped',%s,'followup',%s)",
                (uuid.uuid4(), OWNER, n + 1000, NOW, NOW),
            )
    h = Harness(store)
    await h.service.step()
    assert h.calls == 0
    assert (await store.contact.snapshot())["last_followup"]["reason"] == "frequency_limit"


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": 1},
        {"min_minutes": float("nan")},
        {"max_minutes": 1},
        {"min_minutes": 30},
        {"daily_limit": 7},
    ],
)
def test_followup_validation(patch):
    with pytest.raises(ValueError):
        validate_followup({**DEFAULT_FOLLOWUP, **patch})
