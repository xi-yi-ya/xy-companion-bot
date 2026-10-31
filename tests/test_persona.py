from datetime import UTC, datetime, timedelta

import pytest

from plugin.memory import TZ
from plugin.persona import build_system_prompt


async def evidence(store, content="我喜欢你说话简短一点", days=0):
    return await store.record(
        "webchat",
        "owner",
        "persona-test",
        str(days) + content,
        "user",
        content,
        datetime.now(UTC) - timedelta(days=days),
    )


async def consistent(profile, traits, proposal):
    return {"consistent": True, "risk": "low", "reason": "符合固定人设，仅为表达习惯变化。"}


async def candidate(
    store, layer="L1", field="speaking_habit", value="优先用简短自然的句子交流", check=consistent
):
    msg = await evidence(store)
    proposal = await store.personas.propose(
        msg["person_id"],
        layer,
        field,
        value,
        "为了更符合长期交流习惯",
        [{"message_id": msg["id"], "quote": msg["content"]}],
        check,
    )
    return msg, proposal


async def test_proposal_never_changes_persona_until_owner_approval(store):
    before = await store.personas.profile()
    msg, p = await candidate(store)
    assert p["status"] == "pending"
    assert await store.personas.active_traits(msg["person_id"]) == []
    assert await store.personas.profile() == before
    await store.archive(datetime.now(UTC) + timedelta(days=40))
    await store.personas.decide(msg["person_id"], p["id"], "approve")
    traits = await store.personas.active_traits(msg["person_id"])
    assert len(traits) == 1 and traits[0]["content"] == p["new_value"]
    await store.archive(datetime.now(UTC) + timedelta(days=400))
    assert await store.personas.active_traits(msg["person_id"]) == traits


async def test_l0_bot_request_requires_manual_approval_and_is_versioned(store):
    before = await store.personas.profile()
    msg, p = await candidate(store, "L0", "name", "新名字")
    assert (await store.personas.profile())["profile"]["name"] == before["profile"]["name"]
    await store.personas.decide(msg["person_id"], p["id"], "approve")
    after = await store.personas.profile()
    assert after["profile"]["name"] == "新名字" and after["version"] == before["version"] + 1
    with pytest.raises(ValueError):
        await store.personas.decide(msg["person_id"], p["id"], "approve")


async def test_rejection_and_failed_consistency_never_apply(store):
    async def conflict(*args):
        return {"consistent": False, "risk": "high", "reason": "单次女仆玩笑与固定身份冲突。"}

    msg, p = await candidate(store, value="永久改成女仆", check=conflict)
    assert p["status"] == "blocked"
    with pytest.raises(ValueError):
        await store.personas.decide(msg["person_id"], p["id"], "approve")
    msg, p = await candidate(store, value="用短句")
    await store.personas.decide(msg["person_id"], p["id"], "reject")
    assert await store.personas.active_traits(msg["person_id"]) == []


async def test_l0_edit_stales_old_requests_and_pauses_approved_l1(store):
    msg, p = await candidate(store)
    await store.personas.decide(msg["person_id"], p["id"], "approve")
    _, pending = await candidate(store, value="多用拟声词")
    current = await store.personas.profile()
    await store.personas.save_profile(
        {**current["profile"], "speech_style": "稳重克制"}, current["version"]
    )
    with pytest.raises(ValueError):
        await store.personas.decide(msg["person_id"], pending["id"], "approve")
    assert await store.personas.active_traits(msg["person_id"]) == []
    trait = (await store.personas.traits(msg["person_id"]))[0]
    assert trait["needs_review"]
    with pytest.raises(ValueError):
        await store.personas.review_trait(msg["person_id"], trait["id"], "resume")


async def test_source_validation_and_field_allowlist(store):
    msg = await evidence(store)
    for layer, field, sources in [
        ("L0", "memory_budget", [{"message_id": msg["id"], "quote": msg["content"]}]),
        ("L1", "worldview", [{"message_id": msg["id"], "quote": msg["content"]}]),
        ("L1", "speaking_habit", [{"message_id": msg["id"], "quote": "编造的话"}]),
    ]:
        with pytest.raises(ValueError):
            await store.personas.propose(
                msg["person_id"], layer, field, "测试", "修改理由", sources, consistent
            )


async def test_daily_requires_cross_day_evidence_and_never_auto_applies(store):
    msg = await evidence(store, days=0)

    async def no_generate(*args):
        raise AssertionError("Single-day joking must not trigger habit inference")

    result = await store.personas.daily(
        msg["person_id"], datetime.now(TZ).date(), no_generate, consistent
    )
    assert result["status"] == "insufficient_evidence"
    # A new calendar job with enough evidence still creates pending proposals only.
    rows = []
    for i in range(6):
        rows.append(await evidence(store, "我喜欢简短一点 " + str(i), days=1 + i // 2))

    async def generate(profile, traits, sample):
        return [
            {
                "field": "speaking_habit",
                "new_value": "简短交流",
                "reason": "用户跨日重复表达偏好",
                "sources": [{"message_id": r["id"], "quote": r["content"]} for r in rows[::2]],
            }
        ]

    result = await store.personas.daily(
        msg["person_id"], datetime.now(TZ).date() - timedelta(days=1), generate, consistent
    )
    assert result["status"] == "complete"
    assert await store.personas.active_traits(msg["person_id"]) == []
    assert (await store.personas.proposals(msg["person_id"]))[0]["status"] == "pending"


async def test_l1_context_changes_stale_other_pending_edits(store):
    msg, first = await candidate(store, value="先回应情绪")
    _, second = await candidate(store, value="常用简短反问")
    await store.personas.decide(msg["person_id"], first["id"], "approve")
    with pytest.raises(ValueError):
        await store.personas.decide(msg["person_id"], second["id"], "approve")
    _, checked_again = await candidate(store, value="常用简短反问")
    assert checked_again["id"] != second["id"]
    assert checked_again["status"] == "pending"


async def test_paused_trait_requires_recheck_and_new_manual_approval(store):
    msg, proposal = await candidate(store)
    await store.personas.decide(msg["person_id"], proposal["id"], "approve")
    trait = (await store.personas.traits(msg["person_id"]))[0]
    before = await store.personas.profile()
    await store.personas.save_profile(
        {**before["profile"], "worldview": "平凡生活中的朋友"}, before["version"]
    )
    p = await store.personas.recheck_trait(msg["person_id"], trait["id"], consistent)
    assert p["status"] == "pending"
    assert not await store.personas.active_traits(msg["person_id"])
    await store.personas.decide(msg["person_id"], p["id"], "approve")
    assert len(await store.personas.active_traits(msg["person_id"])) == 1


async def test_budget_edits_do_not_write_or_pause_persona(store):
    msg, p = await candidate(store)
    await store.personas.decide(msg["person_id"], p["id"], "approve")
    before = await store.personas.profile()
    await store.save_budgets({"memory_budget": 2100, "history_budget": 4100})
    assert await store.personas.profile() == before
    assert len(await store.personas.active_traits(msg["person_id"])) == 1


async def test_concurrent_l0_edit_during_model_check_cannot_leave_pending_request(store):
    async def edit_during_check(profile, traits, proposal):
        before = await store.personas.profile()
        await store.personas.save_profile(
            {**before["profile"], "worldview": "另一段背景"}, before["version"]
        )
        return await consistent(profile, traits, proposal)

    msg, p = await candidate(store, check=edit_during_check)
    assert p["status"] == "stale"
    with pytest.raises(ValueError):
        await store.personas.decide(msg["person_id"], p["id"], "approve")


def test_prompt_declares_priority_and_contains_only_passed_approved_habits():
    profile = {
        "name": "小伴",
        "personality": "温和",
        "speech_style": "自然",
        "boundaries": "不虚构经历",
        "worldview": "日常陪伴",
    }
    text = build_system_prompt(
        profile, [{"field": "addressing_user", "content": "称用户为阿白"}], "L2 仅是事实参考"
    )
    assert "L0 > L1 > L2" in text
    assert text.index("<L0_FIXED_PERSONA>") < text.index("<L1_APPROVED_HABITS>")
    assert "阿白" in text and "未批准的申请不属于你的人设" in text
