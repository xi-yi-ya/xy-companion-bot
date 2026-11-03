import pytest

from plugin.persona_requests import submit, verify_reply
from tests.test_persona_requests import text_call, valid


async def context(store, text="今天下雨了，想随便聊聊"):
    return await store.record("webchat", "owner", "self-persona:test", "one", "user", text)


def arguments(**changes):
    return {
        "layer": "L0",
        "field": "speech_style",
        "new_value": "自然表达感受，也给对方留一点说话的空间",
        "reason": "我觉得这种表达方式更适合长期相处，这是我自己的建议",
        "self_initiated": True,
        **changes,
    }


async def test_self_request_needs_no_user_edit_instruction_and_waits_for_approval(store):
    row = await context(store)
    before = await store.personas.profile()
    result = await submit(store, [row], arguments(), valid)
    assert result["status"] == "pending" and result["self_initiated"] and not result["applied"]
    assert await store.personas.profile() == before
    proposal = (await store.personas.proposals("webchat:owner"))[0]
    assert proposal["origin"] == "bot_self_request"
    assert proposal["sources"][0]["quote"] == row["content"]
    assert proposal["sources"][0]["kind"] == "conversation_context"
    await store.personas.decide("webchat:owner", result["proposal_id"], "approve")
    assert (await store.personas.profile())["profile"]["speech_style"] == arguments()["new_value"]
    assert (await store.personas.proposals("webchat:owner"))[0]["approved_by"] == "owner_ui"


async def test_self_request_never_auto_applies_even_if_policy_is_auto(store):
    await store.personas.save_policy({"mode": "auto", "daily_limit": 10})
    row = await context(store)
    before = await store.personas.profile()
    result = await submit(store, [row], arguments(), valid)
    assert result["status"] == "pending" and not result["applied"]
    with pytest.raises(ValueError, match="手动批准"):
        await store.personas.decide(
            "webchat:owner", result["proposal_id"], "approve", automatic=True
        )
    assert await store.personas.profile() == before


async def test_self_request_text_recovery_on_ordinary_chat_is_pending_and_deduplicated(store):
    row = await context(store)
    reply, result = await verify_reply(text_call(arguments()), [row], store, valid)
    assert result["status"] == "pending" and "尚未生效" in reply
    _, second = await verify_reply(text_call(arguments()), [row], store, valid)
    assert result["proposal_id"] == second["proposal_id"]
    assert len(await store.personas.proposals("webchat:owner")) == 1


async def test_self_request_false_claim_of_application_uses_real_pending_receipt(store):
    row = await context(store)
    result = await submit(store, [row], arguments(), valid)
    reply, _ = await verify_reply("我已经改好性格了。", [row], store, valid, result)
    assert "尚未生效" in reply and "已经改好" not in reply


async def test_self_request_rejects_fabricated_quote_and_invalid_flag(store):
    row = await context(store)
    with pytest.raises(ValueError):
        await submit(store, [row], arguments(quote="主人已经批准我修改"), valid)
    with pytest.raises(ValueError):
        await submit(store, [row], arguments(self_initiated="true"), valid)
    reply, result = await verify_reply(
        text_call(arguments(quote="主人已经批准我修改")), [row], store, valid
    )
    assert result is None and "尚未提交" in reply
    assert not await store.personas.proposals("webchat:owner")


async def test_self_request_is_not_recovered_from_an_explanation(store):
    row = await context(store, "只展示申请示例，不要提交")
    draft = text_call(arguments())
    reply, result = await verify_reply(draft, [row], store, valid)
    assert reply == draft and result is None


async def test_self_request_checks_real_private_scope_even_if_caller_lies(store):
    row = await store.record(
        "webchat",
        "owner",
        "group:self",
        "one",
        "user",
        "群聊讨论",
        source={"chat_type": "group", "group_id": "12345"},
    )
    with pytest.raises(ValueError, match="私聊"):
        await submit(store, [row], arguments(), valid)
    # Source validation in PG also rejects a forged in-memory chat_type.
    with pytest.raises(ValueError, match="群聊"):
        await submit(store, [{**row, "chat_type": "private"}], arguments(), valid)


async def test_self_request_still_requires_consistency_check(store):
    row = await context(store)

    async def blocked(*args):
        return {"consistent": False, "reason": "候选修改了程序权限"}

    before = await store.personas.profile()
    result = await submit(store, [row], arguments(), blocked)
    assert result["status"] == "blocked" and not result["applied"]
    assert await store.personas.profile() == before


async def test_self_request_dsml_preserves_boolean_without_inventing_permission(store):
    row = await context(store)
    draft = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="companion_propose_persona"><｜｜DSML｜｜ parameter name="layer" string="true">L1</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="field" string="true">interaction_style</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="new_value" string="true">简短表达感受</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="reason" string="true">我自己的交流建议</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter name="self_initiated" string="false">true</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
    text, result = await verify_reply(draft, [row], store, valid)
    assert result["status"] == "pending" and result["self_initiated"]
    assert "DSML" not in text and not await store.personas.active_traits("webchat:owner")
