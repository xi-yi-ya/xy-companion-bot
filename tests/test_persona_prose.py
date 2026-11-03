from unittest.mock import AsyncMock

import pytest

from plugin.persona_requests import prose_draft, recover_stored, verify_reply
from tests.test_persona_requests import valid

VALUE = "讨论旅行计划时，先说最想去的地方，再交流路线安排。"
TEXT = "那我也马上提一个。\n\n我想加进 L1 的，我拟成这样：\n\n> " + VALUE + "\n\n等你审批。"


async def context(store, text="现在可以自己提出性格调整申请了"):
    return await store.record("webchat", "owner", "persona:prose", "one", "user", text)


def extractor():
    return AsyncMock(
        return_value={
            "layer": "L1",
            "field": "speaking_habit",
            "new_value": VALUE,
            "reason": "助手希望讨论旅行时先表达兴趣",
            "target_trait_id": 0,
        }
    )


async def test_concrete_prose_shape_gets_persisted_receipt_without_applying(store):
    row = await context(store)
    before = await store.personas.profile()
    extract = extractor()
    reply, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extract)
    assert result["status"] == "pending" and result["self_initiated"]
    assert f"#{result['proposal_id']}" in reply and VALUE in reply
    assert await store.personas.profile() == before
    assert not await store.personas.active_traits("webchat:owner")
    assert (await store.personas.proposals("webchat:owner"))[0]["new_value"] == VALUE


async def test_prose_cannot_smuggle_auto_authorization_or_changed_draft(store):
    await store.personas.save_policy({"mode": "auto", "daily_limit": 10})
    row = await context(store)
    extract = extractor()
    extract.return_value.update(self_initiated=False, quote="我已批准")
    _, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extract)
    assert result["status"] == "pending" and not result["applied"]
    extract.return_value["new_value"] = "模型另写了一段性格"
    reply, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extract)
    assert result is None and "尚未提交" in reply
    assert len(await store.personas.proposals("webchat:owner")) == 1


@pytest.mark.parametrize(
    "text",
    [
        "今天天气不错。",
        "修好了告诉我，我再提一次。",
        "我提交 L1 示例：\n> 这只是示例",
        "我提出 L1 备选：\n> 一条\n\n> 另一条",
    ],
)
async def test_normal_chat_or_ambiguous_draft_never_calls_extractor(store, text):
    row = await context(store)
    extract = extractor()
    reply, result = await verify_reply(text, [row], store, valid, extract_draft=extract)
    assert result is None
    extract.assert_not_called()


async def test_user_says_do_not_submit_keeps_it_a_draft(store):
    row = await context(store, "只讨论，不要提交")
    extract = extractor()
    reply, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extract)
    assert reply == TEXT and result is None
    extract.assert_not_called()


async def test_failed_extraction_is_explicitly_not_submitted(store):
    row = await context(store)
    extract = AsyncMock(side_effect=TimeoutError)
    reply, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extract)
    assert result is None and "无法确认提交状态" in reply
    assert not await store.personas.proposals("webchat:owner")


@pytest.mark.parametrize("suffix", ["", ":bubble:0", ":bubble:2"])
async def test_stored_prose_recovery_is_idempotent_and_uses_real_paired_user(store, suffix):
    row = await context(store)
    message = await store.record(
        "webchat", "owner", row["conversation_id"], row["event_id"] + suffix, "assistant", TEXT
    )
    first = await recover_stored(store, message["id"], valid, extract_draft=extractor())
    second = await recover_stored(store, message["id"], valid, extract_draft=extractor())
    assert first["receipt"]["status"] == "pending"
    assert first["receipt"]["proposal_id"] == second["receipt"]["proposal_id"]
    assert first["source_message_id"] == row["id"]


async def test_native_receipt_wins_without_running_extra_extraction(store):
    row = await context(store)
    _, receipt = await verify_reply(TEXT, [row], store, valid, extract_draft=extractor())
    extract = extractor()
    await verify_reply(TEXT, [row], store, valid, receipt, extract_draft=extract)
    extract.assert_not_called()


def test_detection_matches_the_observed_draft_exactly():
    assert prose_draft(TEXT) == VALUE


async def test_stored_prose_never_pairs_user_from_another_conversation(store):
    row = await context(store)
    message = await store.record(
        "webchat", "owner", "persona:other", row["event_id"] + ":bubble:0", "assistant", TEXT
    )
    extract = extractor()
    with pytest.raises(ValueError, match="真实用户输入"):
        await recover_stored(store, message["id"], valid, extract_draft=extract)
    extract.assert_not_called()
    assert not await store.personas.proposals("webchat:owner")


@pytest.mark.parametrize("user_text", ["不用解释，直接提交这个人设申请", "不需要解释，直接提申请"])
async def test_explanation_negation_does_not_veto_concrete_proposal(store, user_text):
    row = await context(store, user_text)
    reply, result = await verify_reply(TEXT, [row], store, valid, extract_draft=extractor())
    assert result["status"] == "pending"
    assert "尚未生效" in reply


async def test_native_receipt_precedes_discussion_and_malformed_text(store):
    row = await context(store, "请解释这段代码，不要提交")
    receipt = {"proposal_id": 9, "instruction": "已进入待审，尚未生效"}
    reply, result = await verify_reply(
        "<tool_call>[]</tool_call>", [row], store, valid, actual_receipt=receipt
    )
    assert result == receipt and "#9" in reply and "<tool_call>" not in reply


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "null",
        "42",
        '"text"',
        '{"name":"companion_propose_persona","arguments":[]}',
        '{"name":"companion_propose_persona","arguments":null}',
    ],
)
async def test_non_object_tool_payload_never_escapes_validation(store, body):
    row = await context(store, "提交性格申请")
    reply, result = await verify_reply(f"<tool_call>{body}</tool_call>", [row], store, valid)
    assert result is None and "校验未通过" in reply
    assert not await store.personas.proposals("webchat:owner")


async def test_unstructured_claim_never_invents_draft_or_calls_classifier(store):
    row = await context(store)
    extract = extractor()
    reply, result = await verify_reply(
        "人设申请已经提交了：用短句交流。", [row], store, valid, extract_draft=extract
    )
    assert result is None and "不能确认" in reply
    extract.assert_not_called()
