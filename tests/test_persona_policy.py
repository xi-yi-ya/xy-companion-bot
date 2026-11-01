import json

from plugin.persona_requests import recover_stored, verify_reply


async def check(*args):
    return {"consistent": True, "reason": "符合原有人设"}


async def draft(
    store,
    text="那就都加，都说了觉得可以加就提出来",
    value="主动联系直接说想法，不例行询问最近怎么样",
):
    source = await store.record("webchat", "owner", "persona-policy:test", text, "user", text)
    markup = (
        "<tool_call>"
        + json.dumps(
            {
                "name": "companion_propose_persona",
                "arguments": {
                    "layer": "L1",
                    "field": "speaking_habit",
                    "new_value": value,
                    "reason": "让联系更自然",
                    "quote": text,
                },
            },
            ensure_ascii=False,
        )
        + "</tool_call>"
    )
    return source, markup


async def test_real_short_followup_is_repaired_and_visible_in_pending(store):
    source, markup = await draft(store)
    reply, result = await verify_reply("我提一次。\n" + markup, [source], store, check)
    assert result["status"] == "pending" and "<tool_call>" not in reply
    assert not await store.personas.active_traits("webchat:owner")
    assistant = await store.record(
        "webchat", "owner", source["conversation_id"], source["event_id"], "assistant", markup
    )
    repaired = await recover_stored(store, assistant["id"], check)
    assert repaired["receipt"]["proposal_id"] == result["proposal_id"]


async def test_auto_applies_consistent_new_candidate_and_records_actor(store):
    await store.personas.save_policy({"mode": "auto", "daily_limit": 4})
    source, markup = await draft(store)
    reply, result = await verify_reply(markup, [source], store, check)
    assert result["status"] == "approved" and result["applied"] and "自动模式" in reply
    assert len(await store.personas.active_traits("webchat:owner")) == 1
    async with store.pool.connection() as conn:
        row = await (
            await conn.execute("SELECT action FROM persona_events ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row["action"] == "auto_approve"


async def test_mode_switch_does_not_apply_old_pending_and_quota_keeps_new_pending(store):
    first, markup = await draft(store)
    _, old = await verify_reply(markup, [first], store, check)
    await store.personas.save_policy({"mode": "auto", "daily_limit": 1})
    assert (await store.personas.proposals("webchat:owner"))[0]["status"] == "pending"
    source, markup = await draft(store, "加上这条习惯", "聊天不必每次长篇大论")
    _, new = await verify_reply(markup, [source], store, check)
    assert new["applied"] and new["proposal_id"] != old["proposal_id"]
    source, markup = await draft(store, "再加一条", "使用表情不必每条都发")
    _, limited = await verify_reply(markup, [source], store, check)
    assert limited["status"] == "pending" and not limited["applied"]


async def test_auto_consistency_failure_never_applies(store):
    await store.personas.save_policy({"mode": "auto", "daily_limit": 4})
    source, markup = await draft(store)

    async def blocked(*args):
        return {"consistent": False, "reason": "与原人设冲突"}

    _, result = await verify_reply(markup, [source], store, blocked)
    assert result["status"] == "blocked" and not await store.personas.active_traits("webchat:owner")


async def test_auto_mode_explaining_persona_call_is_never_a_request(store):
    await store.personas.save_policy({"mode": "auto", "daily_limit": 4})
    source, markup = await draft(store, "解释这段人设申请示例，不要执行")
    _, result = await verify_reply(markup, [source], store, check)
    assert result is None and not await store.personas.proposals("webchat:owner")
