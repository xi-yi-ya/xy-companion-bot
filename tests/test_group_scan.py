import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from plugin.group_scan import GroupScanner, parse_decision, scan_payload
from plugin.qq_chat import (
    QQIngress,
    QuietWindow,
    group_key,
    raw_content,
    record_account,
    record_platform,
)
from plugin.qq_policy import (
    DEFAULT_QQ_CHAT,
    QQChatPolicy,
    choose_quote,
    quote_target,
    recover_quote_reply,
    validate_qq_chat,
)
from tests.test_qq_chat import Bridge, Event, at, fake_capture


class Policy:
    def __init__(self, **changes):
        self.settings = {**DEFAULT_QQ_CHAT, **changes}

    async def get(self):
        return dict(self.settings)


async def test_unmentioned_group_is_admitted_but_self_and_unselected_groups_are_not():
    policy = Policy(group_ids=["12345"])
    ingress = QQIngress(Bridge(), fake_capture, policy=policy)
    assert await ingress.authorize(Event(group="12345", sender="999")) == "allow"
    assert await ingress.authorize(Event(group="12345", sender="456")) == "ignore"
    assert await ingress.authorize(Event(group="67890")) == "ignore"
    assert await ingress.authorize(Event(sender="999")) == "ignore"
    policy.settings["group_scan_enabled"] = False
    assert await ingress.authorize(Event(group="12345")) == "ignore"
    assert await ingress.authorize(Event(group="12345", components=[at()])) == "allow"
    await ingress.close()


async def test_scan_combines_different_speakers_without_waiting_for_quiet():
    ingress = QQIngress(Bridge(), fake_capture, policy=Policy(group_scan_interval=0.09))
    events = [
        Event(f"成员{i}的话", group="12345", sender=str(100 + i), message_id=str(i))
        for i in range(1, 4)
    ]
    tasks = []
    for event in events:
        tasks.append(asyncio.create_task(ingress.process(event)))
        await asyncio.sleep(0.015)
    assert await asyncio.gather(*tasks) == [False, False, True]
    final = events[-1]
    assert final.is_at_or_wake_command
    assert "QQ 101" in final.message_str and "QQ 103" in final.message_str
    assert len(final.get_extra("companion_batch")) == 3
    assert len({record_account(e) for e in events}) == 1
    assert all("群成员" in raw_content(e) for e in events)
    assert not ingress.groups.pending and not ingress.groups.tasks
    await ingress.close()


async def test_fixed_ticks_do_not_extend_on_a_continuous_group_conversation():
    window = QuietWindow(key=group_key, interval=0.07)
    tasks = []
    for i in range(8):
        event = Event(str(i), group="12345", sender=str(100 + i), message_id=str(i))
        tasks.append(asyncio.create_task(window.collect(event, fake_capture)))
        await asyncio.sleep(0.02)
    batches = [r for r in await asyncio.gather(*tasks) if r]
    assert len(batches) >= 2 and sum(len(b) for b in batches) == 8
    assert not window.pending
    await window.close()


async def test_slow_first_pg_write_cannot_reverse_group_message_order(store):
    window = QuietWindow(key=group_key, interval=0.08)

    async def capture(event):
        if event.message_obj.message_id == "1":
            await asyncio.sleep(0.04)
        return await store.record(
            "qqgroup:qa:12345",
            "room:456",
            "qa:order",
            event.message_obj.message_id,
            "user",
            event.message_str,
        )

    tasks = []
    for i in range(1, 3):
        tasks.append(
            asyncio.create_task(
                window.collect(Event(str(i), group="12345", message_id=str(i)), capture)
            )
        )
        await asyncio.sleep(0.001)
    batches = [r for r in await asyncio.gather(*tasks) if r]
    entries = [e for b in batches for e in b]
    assert [e.row["content"] for e in entries] == ["1", "2"]
    assert entries[0].row["id"] < entries[1].row["id"]
    await window.close()


async def test_different_groups_are_never_combined_and_close_cancels_pending_inputs():
    window = QuietWindow(key=group_key, interval=0.02)
    batches = await asyncio.gather(
        *(window.collect(Event(group=g), fake_capture) for g in ("12345", "67890"))
    )
    assert [len(b) for b in batches] == [1, 1]
    window.configure_interval(20)
    pending = asyncio.create_task(window.collect(Event(group="12345"), fake_capture))
    await asyncio.sleep(0.01)
    await window.close()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_new_private_input_supersedes_a_reply_that_is_already_generating():
    captured = []

    async def capture(event):
        row = await fake_capture(event)
        captured.append(row)
        return row

    ingress = QQIngress(Bridge(), capture, policy=Policy(private_quiet_seconds=0.02))
    first = Event("然后", message_id="1")
    assert await ingress.process(first)
    assert ingress.current(first)
    second = Event("我还想给你说件事", message_id="2")
    pending = asyncio.create_task(ingress.process(second))
    await asyncio.sleep(0.005)
    assert not ingress.current(first)  # Even before the next collection window closes.
    assert await pending and ingress.current(second)
    assert [r["content"] for r in captured] == ["然后", "我还想给你说件事"]
    assert ingress.current(Event(sender="other"))
    await ingress.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"group_scan_interval": True},
        {"group_scan_interval": 0},
        {"group_scan_interval": 601},
        {"group_scan_enabled": "true"},
        {"group_ids": ["not-a-group"]},
        {"quote_mode": "random"},
    ],
)
def test_invalid_scan_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_qq_chat({**DEFAULT_QQ_CHAT, **changes})


async def test_policy_is_persistent_and_leaves_persona_untouched(store):
    before = await store.personas.profile()
    value = {
        **DEFAULT_QQ_CHAT,
        "group_scan_interval": 23,
        "group_ids": ["12345"],
        "quote_mode": "never",
    }
    assert await store.qq_chat.save(value) == value
    assert await QQChatPolicy(store.pool).get() == value
    assert await store.personas.profile() == before


async def captured_group_event(store):
    event = Event("我也想听听其他人的看法", group="12345", sender="123", message_id="scan-1")
    event.set_extra("companion_group_scan", True)
    row = await store.record(
        record_platform(event),
        record_account(event),
        "qa:group-scan",
        "scan-1",
        "user",
        raw_content(event),
    )
    event.set_extra("companion_message", row)
    event.set_extra("companion_batch", [SimpleNamespace(event=event, row=row)])
    return event


@pytest.mark.parametrize(
    "text,expected,status",
    [
        ('{"reply":false,"reason":"成员之间互聊"}', False, "silent"),
        ('{"reply":true,"reason":"自然向 Bot 提问"}', True, "reply"),
        ('{"reply":"true"}', False, "error"),
        ("not json", False, "error"),
    ],
)
async def test_decisions_are_recorded_without_reaching_private_history(
    store, text, expected, status
):
    await store.record(
        "webchat", "owner", "private", "private-1", "user", "PRIVATE_SECRET_不该出现在群聊"
    )
    event = await captured_group_event(store)
    called = []

    async def generate(system, payload, source):
        assert "PRIVATE_SECRET" not in str(payload)
        assert source["person_id"] != "webchat:owner"
        assert payload["messages"][-1]["new"] is True
        called.append(payload)
        return text

    scanner = GroupScanner(store, generate)
    assert await scanner.choose(event) is expected
    assert len(called) == 1
    rows = await scanner.recent()
    assert len(rows) == 1 and rows[0]["decision"] == status
    async with store.pool.connection() as conn:
        assert not await (
            await conn.execute("SELECT 1 FROM messages WHERE role='assistant'")
        ).fetchone()


async def test_explicit_at_skips_the_extra_decision_call_and_disable_takes_effect(store):
    event = await captured_group_event(store)
    event.set_extra("companion_directed", True)

    async def not_called(*args):
        raise AssertionError("No model should be called")

    scanner = GroupScanner(store, not_called)
    assert await scanner.choose(event)
    await store.qq_chat.save({**DEFAULT_QQ_CHAT, "group_scan_enabled": False})
    assert not await scanner.choose(event)
    assert (await scanner.recent())[0]["decision"] == "silent"


async def test_calling_bot_by_name_triggers_a_reply_without_at_or_extra_classifier(store):
    event = await captured_group_event(store)
    event.message_str = "小伴，帮我看一下这个技术问题"

    async def not_called(*args):
        raise AssertionError("An explicit question must not be filtered out by another model")

    assert await GroupScanner(store, not_called).choose(event)


def test_scan_prompt_is_bounded_and_preserves_roles_and_speaker_labels():
    rows = [
        dict(
            id=i,
            role="assistant" if i % 3 == 0 else "user",
            created_at=datetime.now(UTC),
            content="[QQ 123] " + "文字" * 3000,
        )
        for i in range(100)
    ]
    payload = scan_payload({"name": "小伴"}, rows, {99}, budget=4000)
    assert payload["messages"][-1]["id"] == 99
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["new"]
    assert len(payload["messages"]) < len(rows)
    assert parse_decision('```json\n{"reply":false}\n```') == (False, "")


def test_optional_quote_selects_real_input_and_never_overrides_off_switch():
    event = Event(message_id="real-2")
    event.set_extra("companion_message", {"id": 102})
    first = Event(message_id="real-1", sender="999")
    event.set_extra(
        "companion_batch",
        [
            SimpleNamespace(event=first, row={"id": 101}),
            SimpleNamespace(event=event, row={"id": 102}),
        ],
    )
    assert quote_target(event, DEFAULT_QQ_CHAT) is None
    choose_quote(event, "101")
    assert quote_target(event, DEFAULT_QQ_CHAT) == {"id": "real-1", "sender": "999"}
    assert quote_target(event, {**DEFAULT_QQ_CHAT, "quote_mode": "never"}) is None
    assert quote_target(event, {**DEFAULT_QQ_CHAT, "quote_mode": "always"})["id"] == "real-2"
    with pytest.raises(ValueError):
        choose_quote(event, "arbitrary-message-id")


@pytest.mark.parametrize("style", ["json", "dsml"])
async def test_textual_quote_call_is_executed_once_without_leaking_markup(style):
    event = Event(message_id="real-1")
    event.set_extra("companion_message", {"id": 101})
    if style == "json":
        text = '<tool_call>{"name":"companion_reply_to","arguments":{"message_id":101}}</tool_call>'
    else:
        text = '<｜DSML｜calls><｜DSML｜invoke name="companion_reply_to"><｜DSML｜parameter name="message_id">101</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜calls>'
    calls = []

    async def continue_reply():
        calls.append(1)
        assert event.get_extra("companion_quote_target")["id"] == "real-1"
        return "是这条里说的明天下午。"

    assert (
        await recover_quote_reply(text, event, DEFAULT_QQ_CHAT, continue_reply)
        == "是这条里说的明天下午。"
    )
    assert calls == [1]


async def test_bad_or_disabled_quote_never_executes_another_tool_or_retries_forever():
    event = Event(message_id="real-1")
    event.set_extra("companion_message", {"id": 101})
    calls = []

    async def broken_reply():
        calls.append(1)
        return '<tool_call>{"name":"companion_reply_to"}</tool_call>'

    invalid = '<tool_call>{"name":"companion_reply_to","arguments":{"message_id":999}}</tool_call>'
    result = await recover_quote_reply(invalid, event, DEFAULT_QQ_CHAT, broken_reply)
    assert (
        "<tool_call>" not in result and not calls and not event.get_extra("companion_quote_target")
    )
    valid = invalid.replace("999", "101")
    result = await recover_quote_reply(
        valid, event, {**DEFAULT_QQ_CHAT, "quote_mode": "never"}, broken_reply
    )
    assert (
        "<tool_call>" not in result and not calls and not event.get_extra("companion_quote_target")
    )
    assert (
        await recover_quote_reply(valid, event, DEFAULT_QQ_CHAT, broken_reply) == "我指的是这条。"
    )
    assert calls == [1]


async def test_normal_reply_and_quote_with_prose_need_no_second_model_call():
    event = Event(message_id="real-1")
    event.set_extra("companion_message", {"id": 101})

    async def not_called():
        raise AssertionError("No unnecessary second call")

    assert await recover_quote_reply("好呀", event, DEFAULT_QQ_CHAT, not_called) == "好呀"
    text = '<tool_call>{"name":"companion_reply_to","arguments":{"message_id":"101"}}</tool_call>这条里说了明天下午。'
    assert (
        await recover_quote_reply(text, event, DEFAULT_QQ_CHAT, not_called)
        == "这条里说了明天下午。"
    )
