import asyncio
import base64
from pathlib import Path

import pytest
from test_qq_chat import Event

from plugin.qq import guard_waking_stage
from plugin.qq_agent import (
    DEFAULT_AGENT,
    QQAgent,
    group_permission,
    routed_arguments,
    validate_agent,
)
from plugin.qq_chat import QQIngress


class Harness:
    def __init__(self, store, tmp_path):
        self.calls = []
        self.identity = {"online": True, "bot_qq": "456789", "owner_qq": "123456"}
        self.hook = None
        self.agent = QQAgent(store.pool, self.call, lambda: self.identity, tmp_path)

    async def call(self, action, **args):
        self.calls.append((action, args))
        if self.hook:
            return await self.hook(action, args)
        if action == "get_group_list":
            return [{"group_id": 987654, "group_name": "test"}]
        return {"message_id": 876543} if action.startswith("send_") else {}

    async def request(self, kind="friend", **updates):
        raw = {
            "post_type": "request",
            "request_type": kind,
            "sub_type": "invite" if kind == "group" else "",
            "self_id": 456789,
            "user_id": 123456,
            "group_id": 987654,
            "flag": "opaque flag",
            "comment": "测试申请",
        }
        return await self.agent.inbox({**raw, **updates}, "companion_qq")

    async def asset(self, name="test.txt"):
        return await self.agent.add_asset(name, base64.b64encode(b"hello").decode())


async def test_unknown_friend_stays_pending_until_real_ui_decision(store, tmp_path):
    h = Harness(store, tmp_path)
    result = await h.request()
    assert result["status"] == "pending" and not h.calls
    assert (await h.agent.handle(result["request_id"], True))["status"] == "approval_required"
    snapshot = await h.agent.snapshot()
    assert "flag" not in snapshot["requests"][0]
    assert (await h.agent.handle(result["request_id"], True, actor="owner_ui"))["status"] == "sent"
    assert (await h.agent.handle(result["request_id"], False, actor="owner_ui"))[
        "status"
    ] == "approved"
    assert len(h.calls) == 1 and h.calls[0][1]["approve"] is True


async def test_allowlist_and_duplicate_event_execute_once(store, tmp_path):
    h = Harness(store, tmp_path)
    await h.agent.save(
        {**DEFAULT_AGENT, "friend_policy": "allowlist", "friend_allowlist": ["123456"]}
    )
    results = await asyncio.gather(h.request(), h.request())
    assert all(r["status"] in ("sent", "unknown", "approved") for r in results)
    assert len(h.calls) == 1


async def test_group_invitation_creates_readonly_room_and_ignores_member_admission(store, tmp_path):
    h = Harness(store, tmp_path)
    await h.agent.save(
        {**DEFAULT_AGENT, "invite_policy": "allowlist", "group_allowlist": ["987654"]}
    )
    assert (await h.request("group", sub_type="add"))["status"] == "unsupported"
    assert (await h.request("group"))["status"] == "sent"
    settings = await h.agent.settings()
    assert group_permission(settings, "987654", "read") and not group_permission(
        settings, "987654", "reply"
    )
    assert h.calls[0][0] == "set_group_add_request" and h.calls[0][1]["sub_type"] == "invite"


async def test_paused_keeps_requests_pending_and_stops_send(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset()
    await h.agent.save(
        {
            **DEFAULT_AGENT,
            "paused": True,
            "friend_policy": "allowlist",
            "friend_allowlist": ["123456"],
        }
    )
    assert (await h.request())["status"] == "pending"
    assert (await h.agent.send_media(asset["asset_id"], "private", "123456", "send"))[
        "status"
    ] == "paused"
    assert not h.calls


async def test_media_receipt_idempotency_and_recall(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset()
    first = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    second = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    assert (
        first["message_id"] == 876543
        and second["operation_id"] == first["operation_id"]
        and len(h.calls) == 1
    )
    assert h.calls[0][1]["message"][0]["type"] == "file"
    assert (await h.agent.recall(first["operation_id"], "recall"))["status"] == "sent"
    assert h.calls[-1] == ("delete_msg", {"message_id": "876543"})
    assert (await h.agent.snapshot())["operations"][-1]["recalled_at"]


async def test_image_segment_and_group_media_permission(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset("image.png")
    with pytest.raises(ValueError, match="附件"):
        await h.agent.send_media(asset["asset_id"], "group", "987654", "send")
    await h.agent.save(
        {
            **DEFAULT_AGENT,
            "group_rules": [{"group_id": "987654", "read": True, "reply": True, "media": True}],
        }
    )
    assert (await h.agent.send_media(asset["asset_id"], "group", "987654", "send"))[
        "status"
    ] == "sent"
    assert h.calls[-1][0] == "send_group_msg" and h.calls[-1][1]["message"][0]["type"] == "image"


async def test_other_recipient_and_path_escape_denied(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset()
    with pytest.raises(ValueError, match="未授权"):
        await h.agent.send_media(asset["asset_id"], "private", "777777", "send")
    async with store.pool.connection() as conn:
        await conn.execute(
            "UPDATE qq_assets SET path=%s WHERE id=%s",
            (str(Path(__file__).resolve()), asset["asset_id"]),
        )
    with pytest.raises(ValueError, match="越界"):
        await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    assert not h.calls


async def test_timeout_is_durable_unknown_and_never_retried(store, tmp_path):
    h = Harness(store, tmp_path)

    async def timeout(action, args):
        raise TimeoutError("ACK lost")

    h.hook = timeout
    asset = await h.asset()
    assert (await h.agent.send_media(asset["asset_id"], "private", "123456", "send"))[
        "status"
    ] == "unknown"
    assert (await h.agent.send_media(asset["asset_id"], "private", "123456", "send"))[
        "status"
    ] == "unknown"
    assert len(h.calls) == 1


async def test_restart_marks_unfinished_operation_unknown(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset()
    result = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    async with store.pool.connection() as conn:
        await conn.execute(
            "UPDATE qq_operations SET status='sending' WHERE id=%s", (result["operation_id"],)
        )
    await h.agent.recover()
    assert (await h.agent.send_media(asset["asset_id"], "private", "123456", "send"))[
        "status"
    ] == "unknown" and len(h.calls) == 1


async def test_recall_other_account_and_unconfirmed_message_denied(store, tmp_path):
    h = Harness(store, tmp_path)
    asset = await h.asset()
    result = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    h.identity["bot_qq"] = "999999"
    with pytest.raises(ValueError):
        await h.agent.recall(result["operation_id"], "recall")
    assert len(h.calls) == 1


async def test_late_qq_notice_can_confirm_recall_after_api_timeout(store, tmp_path):
    from aiocqhttp.exceptions import ActionFailed

    h = Harness(store, tmp_path)
    asset = await h.asset()
    sent = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")

    async def delayed(action, args):
        assert action == "delete_msg"
        await h.agent.notice(
            {
                "post_type": "notice",
                "notice_type": "friend_recall",
                "self_id": 456789,
                "user_id": 456789,
                "message_id": 876543,
            }
        )
        raise ActionFailed({"retcode": 1200, "message": "callback timeout"})

    h.hook = delayed
    result = await h.agent.recall(sent["operation_id"], "recall")
    assert result["status"] == "sent" and result["confirmed_by"] == "QQ_notice"


async def test_native_send_callback_timeout_is_not_reported_as_definitely_unsent(store, tmp_path):
    from aiocqhttp.exceptions import ActionFailed

    h = Harness(store, tmp_path)

    async def timeout(action, args):
        raise ActionFailed({"retcode": 1200, "message": "Timeout: NodeIKernelMsgService/sendMsg"})

    h.hook = timeout
    asset = await h.asset()
    result = await h.agent.send_media(asset["asset_id"], "private", "123456", "send")
    assert result["status"] == "unknown" and "retcode=1200" in result["error"]
    assert (await h.agent.send_media(asset["asset_id"], "private", "123456", "send"))[
        "status"
    ] == "unknown" and len(h.calls) == 1


def test_permissions_are_consistent():
    assert not group_permission(DEFAULT_AGENT, "987654", "read")
    with pytest.raises(ValueError):
        validate_agent(
            {
                **DEFAULT_AGENT,
                "group_rules": [
                    {"group_id": "987654", "read": False, "reply": True, "media": False}
                ],
            }
        )


async def test_request_is_captured_before_owner_only_wake_gate(store, tmp_path):
    h = Harness(store, tmp_path)

    class Bridge:
        settings = {"enabled": True, "owner_qq": "123456"}

        async def authorize(self, *args):
            raise AssertionError("申请不能经过普通私聊授权或模型")

    class Stage:
        async def process(self, event):
            raise AssertionError("申请不能进入原生消息唤醒")

    async def capture(event):
        raise AssertionError("申请不能冒充聊天记录")

    ingress = QQIngress(Bridge(), capture)
    ingress.agent = h.agent
    guard_waking_stage(Stage, ingress.bridge, ingress)
    event = Event(sender="777777")
    event.message_obj.raw_message = {
        "post_type": "request",
        "request_type": "friend",
        "self_id": 456789,
        "user_id": 777777,
        "flag": "incoming-new",
        "comment": "hi",
    }
    await Stage().process(event)
    assert event.stopped and (await h.agent.snapshot())["pending"] == 1 and not h.calls


async def test_initial_groups_are_migrated_once_and_later_unknown_groups_are_denied(
    store, tmp_path
):
    h = Harness(store, tmp_path)
    await h.agent.ensure_inventory()
    assert group_permission(await h.agent.settings(), "987654", "reply")
    h.agent.known_accounts.clear()
    h.calls.clear()
    await h.agent.ensure_inventory()
    assert not h.calls and not group_permission(await h.agent.settings(), "999999", "read")
    await h.agent.notice(
        {
            "post_type": "notice",
            "notice_type": "group_increase",
            "user_id": 456789,
            "group_id": 999999,
        }
    )
    settings = await h.agent.settings()
    assert group_permission(settings, "999999", "read") and not group_permission(
        settings, "999999", "reply"
    )


async def test_old_wake_wrapper_is_replaced_on_reload_without_double_authorization():
    calls = []

    class Stage:
        async def process(self, event):
            calls.append("native")

    original = Stage.process

    async def legacy(stage, event):
        calls.append("old")
        return await original(stage, event)

    Stage.process = legacy
    Stage._companion_qq_guard = True

    class Bridge:
        async def authorize(self, *args):
            calls.append("new")
            return "allow"

    guard_waking_stage(Stage, Bridge())
    guard_waking_stage(Stage, Bridge())
    await Stage().process(Event())
    assert calls == ["new", "native"]


async def test_framework_text_sends_are_receipted_and_stopped_by_emergency(store, tmp_path):
    h = Harness(store, tmp_path)
    calls = []

    class Bot:
        async def call_action(self, action, **args):
            calls.append((action, args))
            return {"message_id": 123123}

    bot = Bot()
    h.agent.call = lambda action, **args: bot.call_action(
        action, **routed_arguments("456789", args)
    )
    h.agent.install_transport(bot)
    assert await bot.call_action(
        "send_private_msg",
        self_id="456789",
        user_id=123456,
        message=[{"type": "text", "data": {"text": "hello"}}],
    ) == {"message_id": 123123}
    snapshot = await h.agent.snapshot()
    assert len(snapshot["operations"]) == 1 and snapshot["operations"][0]["actor"] == "framework"
    await h.agent.save({**DEFAULT_AGENT, "paused": True})
    with pytest.raises(ValueError):
        await bot.call_action("send_private_msg", user_id=123456, message=[])
    assert len(calls) == 1
    await h.agent.close()


async def test_transport_reload_handoff_does_not_stack_or_use_old_agent(store, tmp_path):
    old = Harness(store, tmp_path / "old")
    new = Harness(store, tmp_path / "new")
    calls = []

    class Bot:
        async def call_action(self, action, **args):
            calls.append(action)
            return {"message_id": 123123}

    bot = Bot()
    native = bot.call_action
    old.agent.call = lambda action, **args: bot.call_action(action, **args)
    new.agent.call = lambda action, **args: bot.call_action(action, **args)
    old.agent.install_transport(bot)
    new.agent.install_transport(bot)
    await old.agent.close()
    await bot.call_action("send_private_msg", user_id=123456, message=[])
    assert calls == ["send_private_msg"]
    await new.agent.close()
    assert bot.call_action == native
