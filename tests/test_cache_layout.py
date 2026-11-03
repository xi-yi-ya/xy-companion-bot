"""Exercise the real injection hooks without loading AstrBot or production PG."""

import ast
import copy
import json
import re
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugin.memory import TZ
from plugin.proactive import OWNER

ROOT = Path(__file__).resolve().parents[1]


class Part:
    def __init__(self, text):
        self.text = text
        self.temporary = False

    def mark_as_temp(self):
        self.temporary = True
        return self


def hook(path, name):
    tree = ast.parse(path.read_text("utf-8"))
    method = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )
    method.decorator_list = []
    for arg in method.args.args:
        arg.annotation = None
    namespace = {
        "SCOPE": ContextVar("test_cache_scope"),
        "OWNER": OWNER,
        "datetime": datetime,
        "TZ": TZ,
        "json": json,
        "re": re,
        "PERSONA_TOOL_RULE": "stable persona approval rule",
        "WEB_POLICY": "stable web policy",
        "TextPart": Part,
        "ToolSet": NS,
        "WebTurn": lambda *args: args,
        "group_permission": lambda settings, group, action: settings["reply"],
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


async def build_request(
    number=1001,
    *,
    platform="aiocqhttp",
    private=True,
    history=None,
    memory="random archive A",
    allowed=True,
    batch=False,
):
    """Synthetic state only; also reused by the opt-in real API cache probe."""
    history = (
        history
        if history is not None
        else [
            {"role": "user", "content": "[历史发言时间 2026-11-03T18:00]一起讨论一个话题"},
            {"role": "assistant", "content": "保留原来的完整回答。"},
        ]
    )
    source = dict(
        id=number,
        person_id=OWNER if private else "group:42",
        platform=platform,
        platform_id="companion_qq",
        chat_type="private" if private else "group",
        sender_id="123456",
        group_id=None if private else "42",
    )
    extras = {"companion_message": source}
    if batch:
        extras["companion_batch"] = [NS(row={"id": number - 1}), NS(row=source)]
    event = NS(
        get_extra=extras.get,
        set_extra=extras.__setitem__,
        is_private_chat=lambda: private,
        get_platform_name=lambda: platform,
        get_group_id=lambda: "42",
        message_str="普通聊天",
        stop_event=lambda: extras.update(stopped=True),
    )
    all_tools = [
        NS(active=True, name=name)
        for name in (
            "companion_search",
            "companion_reply_to",
            "companion_propose_persona",
            "companion_qq_directory",
            "companion_qzone_status",
            "companion_sticker_collect",
            "companion_web_search",
            "unrelated_tool",
        )
    ]
    state = {"settings": {"reply": allowed, "paused": False}, "pending": number}
    main = NS(
        qq_ingress=NS(current=lambda e: True),
        meter_provider=AsyncMock(),
        companion_context=AsyncMock(
            return_value=("L0 fixed persona > L1 > L2", memory, history, {})
        ),
        store=NS(
            policy=NS(get=AsyncMock(return_value={"retrieval_budget": 2000})),
            personas=NS(policy=AsyncMock(return_value={"mode": "confirm", "daily_limit": 5})),
            qq_chat=NS(get=AsyncMock(return_value={"quote_mode": "adaptive"})),
        ),
        qq_custom=NS(settings=AsyncMock(return_value=({"modes": {"collect": "off"}}, None))),
        qq_agent=NS(
            snapshot=AsyncMock(return_value=state),
            settings=AsyncMock(return_value=state["settings"]),
        ),
        qq=NS(settings={"owner_qq": "123456"}),
        qzone_capabilities=AsyncMock(
            return_value={"qq_online": number % 2 == 0, "last_read": {"due_at": number}}
        ),
        rhythm=NS(settings=AsyncMock(return_value={"enabled": True})),
        web=NS(
            settings=AsyncMock(
                return_value={"enabled": True, "max_calls": 3, "context_budget": 2000}
            )
        ),
        context=NS(get_llm_tool_manager=lambda: NS(get_full_tool_set=lambda: NS(tools=all_tools))),
        qzone_owner_event=lambda e: private,
    )
    req = NS(contexts=[], system_prompt="", extra_user_content_parts=[], func_tool=None)
    await hook(ROOT / "plugin/main.py", "inject")(main, event, req)
    return req, main, event, history, extras


@pytest.mark.parametrize(
    "platform,private", [("aiocqhttp", True), ("webchat", True), ("aiocqhttp", False)]
)
async def test_changing_turn_state_preserves_system_and_full_history_prefix(platform, private):
    first, _, _, original, _ = await build_request(platform=platform, private=private)
    before = copy.deepcopy(original)
    extended = [
        *original,
        {"role": "user", "content": "第二轮完整输入"},
        {"role": "assistant", "content": "第二轮完整回答"},
    ]
    second, _, _, _, _ = await build_request(
        1002, platform=platform, private=private, history=extended, memory="random archive B"
    )
    assert first.system_prompt == second.system_prompt
    assert first.contexts[:-1] == second.contexts[: len(original)] == before
    assert second.contexts[:-1] == extended
    assert original == before and len(extended) == 4
    assert first.contexts[-1]["role"] == second.contexts[-1]["role"] == "system"
    assert "本轮消息来源" in second.contexts[-1]["content"]
    assert "random archive A" in first.extra_user_content_parts[-1].text
    assert "random archive B" in second.extra_user_content_parts[-1].text
    assert all(p.temporary for p in second.extra_user_content_parts)
    if private:
        assert "当前 QQ 待处理申请数：1002" in second.contexts[-1]["content"]
        assert '"due_at": 1002' in second.contexts[-1]["content"]
        assert "1002" not in second.system_prompt
        assert '"mode": "confirm"' in second.system_prompt
        assert '"owner_qq": "123456"' in second.system_prompt
    if platform == "aiocqhttp":
        assert "可选本轮消息编号：1002" in second.contexts[-1]["content"]
        assert "可选本轮消息编号：1001" not in str(second.contexts)
    else:
        assert "本地 Web 聊天" in second.contexts[-1]["content"]
        assert "可选本轮消息编号" not in str(second.contexts)


async def test_group_scope_and_tool_permissions_remain_separate_from_owner():
    req, main, _, _, _ = await build_request(private=False)
    names = {t.name for t in req.func_tool.tools}
    assert names == {"companion_search", "companion_reply_to", "companion_web_search"}
    assert '"group_id": "42"' in req.contexts[-1]["content"]
    assert "公开群聊" in req.system_prompt
    main.qzone_capabilities.assert_not_awaited()
    main.qq_agent.snapshot.assert_not_awaited()


async def test_denied_group_never_builds_or_sends_request():
    req, _, _, _, extras = await build_request(private=False, allowed=False)
    assert extras["stopped"] and req.func_tool is None
    assert "companion_qq_request" not in extras


async def test_batch_quote_ids_are_current_and_after_history():
    req, _, _, history, _ = await build_request(2222, batch=True)
    assert req.contexts[:-1] == history
    assert "可选本轮消息编号：2221,2222" in req.contexts[-1]["content"]
    assert "2221" not in req.system_prompt and "2222" not in req.system_prompt
    assert "companion_reply_to" in {t.name for t in req.func_tool.tools}


async def test_permission_changes_still_update_leading_rules():
    req, main, event, _, _ = await build_request()
    old_prompt = req.system_prompt
    main.qq_agent.snapshot.return_value["settings"]["paused"] = True
    await hook(ROOT / "plugin/main.py", "inject")(main, event, req)
    assert old_prompt != req.system_prompt and '"paused": true' in req.system_prompt
    # The hook rebuilds contexts from PG, rather than accumulating old runtime data.
    assert len(req.contexts) == 3


async def test_comfy_job_changes_do_not_invalidate_chat_prefix_or_mutate_history():
    path = ROOT / "extensions/companion-comfy-extension/plugin/main.py"
    if not path.is_file():
        pytest.skip("optional local Comfy extension is not installed")
    inject = hook(path, "inject_capabilities")
    requests = []
    for number in (1, 2):
        req, main, event, _, _ = await build_request(number)
        prefix = copy.deepcopy(req.contexts)
        original = req.contexts
        addon = NS(
            companion=lambda: main,
            store=NS(
                jobs=lambda **kw: [
                    {
                        "id": str(number),
                        "status": "queued" if number == 1 else "done",
                        "payload": {"workflow": "image", "prompt": "synthetic art job"},
                    },
                ]
            ),
        )
        await inject(addon, event, req)
        assert original == prefix and req.contexts[:-1] == prefix
        assert req.contexts[-1]["role"] == "system"
        requests.append(req)
    assert requests[0].system_prompt == requests[1].system_prompt
    assert requests[0].contexts[-1] != requests[1].contexts[-1]
    assert "最新创作任务" not in requests[0].system_prompt
