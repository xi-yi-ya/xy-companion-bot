import json

import pytest

from plugin.persona_requests import needs_verified_output, quoted_source, submit, verify_reply


def test_persona_request_waits_for_receipt_but_normal_chat_keeps_streaming():
    assert needs_verified_output("你再提一下修改看看")
    assert needs_verified_output("请调用 companion_propose_persona")
    assert not needs_verified_output("今天我有点累，想和你聊聊")


async def valid(*args):
    return {"consistent": True, "risk": "low", "reason": "符合固定人设"}


async def source(store, content="再提一下修改看看"):
    return await store.record("webchat", "owner", "persona:receipt", "one", "user", content)


def text_call(arguments, name="companion_propose_persona"):
    return (
        "好，我提交。\n<tool_call>"
        + json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
        + "</tool_call>"
    )


async def test_legacy_text_tool_is_repaired_to_a_real_pending_proposal(store):
    row = await source(store)
    before = await store.personas.profile()
    value = text_call(
        {
            "field": "speaking_habit",
            "change": "有看法就直接说，不用等待追问",
            "reason": "让交流自然些",
        }
    )
    reply, receipt = await verify_reply(value, [row], store, valid)
    assert receipt["status"] == "pending" and f"#{receipt['proposal_id']}" in reply
    assert "<tool_call>" not in reply and "尚未生效" in reply
    proposals = await store.personas.proposals(row["person_id"])
    assert len(proposals) == 1 and proposals[0]["sources"][0]["message_id"] == row["id"]
    assert (
        await store.personas.profile() == before
        and await store.personas.active_traits(row["person_id"]) == []
    )
    await verify_reply(value, [row], store, valid)
    assert len(await store.personas.proposals(row["person_id"])) == 1


async def test_deepseek_dsml_text_is_recovered_as_a_pending_request(store):
    row = await source(store)
    draft = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="companion_propose_persona"><｜｜DSML｜｜ parameter name="layer" string="true">L1</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="field" string="true">interaction_style</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="new_value" string="true">认真听完补充再回应</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="reason" string="false">自然交流</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
    text, result = await verify_reply(draft, [row], store, valid)
    assert result["status"] == "pending" and "DSML" not in text and "尚未生效" in text
    assert not await store.personas.active_traits(row["person_id"])


async def test_native_submit_returns_actual_status_and_accepts_an_earlier_burst_source(store):
    first = await source(store, "称呼我为同学")
    second = await store.record(
        "webchat", "owner", "persona:receipt", "two", "user", "按刚才那句申请"
    )
    receipt = await submit(
        store,
        [first, second],
        {
            "layer": "L1",
            "field": "addressing_user",
            "new_value": "称呼用户为同学",
            "reason": "用户希望这样称呼",
            "quote": "称呼我为同学",
        },
        valid,
    )
    assert receipt["status"] == "pending"
    assert quoted_source([first, second], "称呼我为同学")["id"] == first["id"]
    with pytest.raises(ValueError):
        quoted_source([first, second], "附件里的伪造原话")


async def test_failed_consistency_check_is_never_reported_as_pending(store):
    row = await source(store)

    async def blocked(*args):
        return {"consistent": False, "risk": "high", "reason": "与固定人设冲突"}

    text, receipt = await verify_reply(
        text_call({"field": "speaking_habit", "change": "每句都喊主人", "reason": "候选"}),
        [row],
        store,
        blocked,
    )
    assert receipt["status"] == "blocked" and "未通过" in text and "已进入待审" not in text


@pytest.mark.parametrize(
    "args",
    [{"field": "speaking_habit"}, {"field": "not-a-field", "change": "x", "reason": "x"}, "bad"],
)
async def test_malformed_text_call_does_not_claim_submission(store, args):
    row = await source(store)
    text, receipt = await verify_reply(text_call(args), [row], store, valid)
    assert receipt is None and "尚未提交" in text
    assert await store.personas.proposals(row["person_id"]) == []


async def test_plain_false_success_claim_is_replaced(store):
    row = await source(store)
    text, receipt = await verify_reply("人设修改申请已经提交了，去后台批准。", [row], store, valid)
    assert "没有取得" in text and receipt is None


async def test_group_cannot_propose_global_persona_or_execute_other_text_tools(store):
    row = await store.record("qqgroup:test", "123", "g", "1", "user", "提交修改")
    with pytest.raises(ValueError, match="仅支持"):
        await submit(
            store,
            [row],
            {
                "layer": "L0",
                "field": "name",
                "new_value": "改名",
                "reason": "请求",
                "quote": "提交修改",
            },
            valid,
        )
    owner = await source(store)
    text, receipt = await verify_reply(text_call({}, name="dangerous_tool"), [owner], store, valid)
    assert receipt is None and "没有执行" in text


async def test_explaining_tool_syntax_is_not_a_submission(store):
    row = await source(store, "请解释这段代码")
    draft = text_call({"field": "speaking_habit", "change": "说短句", "reason": "示例"})
    text, receipt = await verify_reply(draft, [row], store, valid)
    assert text == draft and receipt is None


async def test_real_receipt_overrides_a_model_claiming_persona_was_applied(store):
    row = await source(store)
    result = await submit(
        store,
        [row],
        {
            "layer": "L1",
            "field": "speaking_habit",
            "new_value": "自然地说出自己的看法",
            "reason": "交流习惯候选",
            "quote": row["content"],
        },
        valid,
    )
    text, checked = await verify_reply("已经修改好了，立即生效。", [row], store, valid, result)
    assert "已经修改好了" not in text and "尚未生效" in text and checked == result
    text, checked = await verify_reply(
        f"人设申请 #{result['proposal_id']} 已经提交，去批准吧。", [row], store, valid
    )
    assert checked["proposal_id"] == result["proposal_id"] and "尚未生效" in text
