import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugin.qq import guard_waking_stage
from plugin.qq_chat import QQIngress, QuietWindow, chat_key, directly_mentioned, record_platform


class Event:
    def __init__(self, text="你好", sender="123", group="", components=None, message_id="1"):
        self.message_str, self.sender, self.group = text, sender, group
        self.message_obj = SimpleNamespace(
            message=components or [],
            message_id=message_id,
            timestamp=1,
            raw_message={},
            self_id="456",
        )
        self.extra, self.stopped, self.sent = {}, False, []

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return "companion_qq"

    def get_self_id(self):
        return "456"

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.message_obj.message

    def is_private_chat(self):
        return not self.group

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key):
        return self.extra.get(key)

    def stop_event(self):
        self.stopped = True

    def plain_result(self, value):
        return value

    async def send(self, value):
        self.sent.append(value)


def at(who="456"):
    return SimpleNamespace(type="At", qq=who)


class Bridge:
    settings = {"enabled": True, "owner_qq": "123"}

    async def authorize(self, account, content):
        return "allow" if account == self.settings["owner_qq"] else "ignore"


async def fake_capture(event):
    return {
        "id": int(event.message_obj.message_id),
        "content": event.message_str,
        "created_at": datetime.now(UTC),
        "person_id": "test",
    }


async def test_empty_typing_notifications_do_not_cancel_real_reply():
    ingress = QQIngress(Bridge(), fake_capture, quiet=0.01)
    try:
        real = Event("真实的一条消息")
        assert await ingress.process(real)
        assert ingress.current(real)
        for _ in range(3):
            notice = Event("", components=[])
            assert await ingress.process(notice)
            assert ingress.current(real)
        command = Event("/源码", message_id="2")
        assert await ingress.process(command)
        assert not ingress.current(real)
    finally:
        await ingress.close()


async def test_three_burst_messages_produce_one_ordered_batch():
    window = QuietWindow(0.04, 0.2)
    tasks = []
    for i in range(1, 4):
        tasks.append(
            asyncio.create_task(window.collect(Event(str(i), message_id=str(i)), fake_capture))
        )
        await asyncio.sleep(0.01)
    result = await asyncio.gather(*tasks)
    assert result[:2] == [None, None]
    assert [e.row["content"] for e in result[-1]] == ["1", "2", "3"]
    assert window.pending == {}
    await window.close()


async def test_group_sender_and_private_sessions_do_not_mix():
    window = QuietWindow(0.01, 0.1)
    events = [Event(group="g1"), Event(group="g2"), Event(group="g1", sender="999"), Event()]
    batches = await asyncio.gather(*(window.collect(e, fake_capture) for e in events))
    assert len({chat_key(e) for e in events}) == 4
    assert all(len(b) == 1 for b in batches)


async def test_slow_record_is_not_dropped_after_window_expires():
    window = QuietWindow(0.02, 0.08)

    async def capture(event):
        if event.message_obj.message_id == "1":
            await asyncio.sleep(0.08)
        return await fake_capture(event)

    first = asyncio.create_task(window.collect(Event(message_id="1"), capture))
    await asyncio.sleep(0.005)
    second = asyncio.create_task(window.collect(Event(message_id="2"), capture))
    rows = await asyncio.gather(first, second)
    assert rows[0] is None and [e.row["id"] for e in rows[1]] == [1, 2]


async def test_db_failure_and_cancelled_input_do_not_poison_other_messages():
    window = QuietWindow(0.04, 0.2)

    async def failed(event):
        raise ValueError("database unavailable")

    with pytest.raises(ValueError):
        await window.collect(Event(), failed)
    batch = await window.collect(Event(message_id="2"), fake_capture)
    assert len(batch) == 1 and batch[0].row["id"] == 2
    task = asyncio.create_task(window.collect(Event(message_id="3"), fake_capture))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await window.close()
    assert not window.pending


async def test_maximum_window_flushes_a_continuous_burst():
    window = QuietWindow(0.05, 0.08)
    tasks = []
    for i in range(6):
        tasks.append(asyncio.create_task(window.collect(Event(message_id=str(i)), fake_capture)))
        await asyncio.sleep(0.025)
    results = [r for r in await asyncio.gather(*tasks) if r]
    assert len(results) >= 2 and sum(len(r) for r in results) == 6


@pytest.mark.parametrize(
    "components,allowed",
    [
        ([at()], True),
        ([at("all")], False),
        ([at("999")], False),
        ([], False),
        ([SimpleNamespace(type="Reply", chain=[at()])], False),
    ],
)
async def test_group_only_accepts_a_real_direct_mention(components, allowed):
    ingress = QQIngress(Bridge(), fake_capture)
    event = Event("@456", group="g1", sender="999", components=components)
    assert directly_mentioned(event) == allowed
    assert (await ingress.authorize(event) == "allow") == allowed


async def test_group_followup_needs_a_pending_mention_from_the_same_sender():
    ingress = QQIngress(Bridge(), fake_capture, quiet=0.05, maximum=0.3)
    start = Event(group="g1", components=[at()], message_id="1")
    assert await ingress.authorize(start) == "allow"
    task = asyncio.create_task(ingress.process(start))
    await asyncio.sleep(0.01)
    followup = Event("还有一句", group="g1", message_id="2")
    assert await ingress.authorize(followup) == "allow"
    assert await ingress.authorize(Event(group="g1", sender="999")) == "ignore"
    assert await ingress.process(followup)
    assert not await task
    assert "还有一句" in followup.message_str and len(followup.get_extra("companion_batch")) == 2
    assert await ingress.authorize(Event(group="g1")) == "ignore"


async def test_bare_at_starts_the_same_quiet_window():
    ingress = QQIngress(Bridge(), fake_capture, quiet=0.04)
    first = Event("", group="g1", components=[at()], message_id="1")
    assert await ingress.authorize(first) == "allow"
    waiting = asyncio.create_task(ingress.process(first))
    await asyncio.sleep(0.01)
    second = Event("刚才叫你是想问个问题", group="g1", message_id="2")
    assert await ingress.authorize(second) == "allow"
    assert await ingress.process(second) and not await waiting
    assert len(second.get_extra("companion_batch")) == 2


async def test_gate_denies_before_capture_even_when_ingress_is_installed():
    captured = []

    async def capture(event):
        captured.append(event)
        return await fake_capture(event)

    class Stage:
        async def process(self, event):
            raise AssertionError("Must not reach native commands")

    ingress = QQIngress(Bridge(), capture, quiet=0.01)
    guard_waking_stage(Stage, Bridge(), ingress)
    event = Event(sender="999")
    await Stage().process(event)
    assert event.stopped and not captured and not event.sent


async def test_group_rows_never_resolve_to_private_owner_or_reset_contact(store):
    private = await store.record("aiocqhttp", "123", "p", "1", "user", "private")
    event = Event(group="g1", components=[at()])
    group = await store.record(record_platform(event), "123", "g", "2", "user", "group")
    assert group["person_id"] != private["person_id"]
    assert "qqgroup:" in group["person_id"]
    assert await store.search_history(group["person_id"], "private") == []


async def test_ingress_preserves_individual_pg_records_and_one_current_prompt(store):
    async def capture(event):
        return await store.record(
            "webchat", "owner", "qq:batch", event.message_obj.message_id, "user", event.message_str
        )

    ingress = QQIngress(Bridge(), capture, quiet=0.04)
    events = [Event("我今天去了公园", message_id="1"), Event("现在心情好多了", message_id="2")]
    tasks = [asyncio.create_task(ingress.process(e)) for e in events]
    assert await asyncio.gather(*tasks) == [False, True]
    winner = events[-1]
    rows = [e.row for e in winner.get_extra("companion_batch")]
    assert [r["content"] for r in rows] == ["我今天去了公园", "现在心情好多了"]
    _, history = await store.context(
        "webchat:owner", rows[-1], before_id=winner.get_extra("companion_before_id")
    )
    assert history == []  # Current batch is in the prompt exactly once.
