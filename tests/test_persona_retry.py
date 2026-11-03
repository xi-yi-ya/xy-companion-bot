import asyncio
from unittest.mock import AsyncMock

import pytest

from plugin.persona_requests import receipt
from tests.test_persona import candidate, consistent, evidence

OWNER = "webchat:owner"


async def failed(*args):
    raise TimeoutError("synthetic provider outage")


async def age(store, proposal):
    async with store.pool.connection() as conn:
        await conn.execute(
            "UPDATE persona_proposals SET updated_at=now()-interval '61 seconds' WHERE id=%s",
            (proposal["id"],),
        )


async def test_transient_failure_retries_same_row_without_auto_approval(store):
    before = await store.personas.profile()
    _, proposal = await candidate(store, check=failed)
    assert proposal["status"] == "blocked" and proposal["check_result"]["retryable"]
    assert "暂时失败" in receipt(proposal)["instruction"]
    with pytest.raises(ValueError, match="60 秒"):
        await store.personas.retry_check(OWNER, proposal["id"], consistent)
    await age(store, proposal)
    await store.personas.save_policy({"mode": "auto", "daily_limit": 10})
    retried = await store.personas.retry_check(OWNER, proposal["id"], consistent)
    assert retried["id"] == proposal["id"] and retried["status"] == "pending"
    assert retried["check_result"]["attempts"] == 2
    assert not retried["check_result"]["retryable"]
    assert retried["sources"] == proposal["sources"] and retried["origin"] == proposal["origin"]
    assert len(await store.personas.proposals(OWNER)) == 1
    assert await store.personas.profile() == before
    assert not await store.personas.active_traits(OWNER)


async def test_retry_budget_is_persisted_and_never_a_background_loop(store):
    _, proposal = await candidate(store, check=failed)
    checker = AsyncMock(side_effect=TimeoutError)
    for attempt in (2, 3):
        await age(store, proposal)
        proposal = await store.personas.retry_check(OWNER, proposal["id"], checker)
        assert proposal["check_result"]["attempts"] == attempt
    await age(store, proposal)
    with pytest.raises(ValueError, match="最多检查 3 次"):
        await store.personas.retry_check(OWNER, proposal["id"], checker)
    assert checker.await_count == 2
    assert "已达 3 次" in receipt(proposal)["instruction"]


async def test_actual_rejection_cannot_be_retried_as_a_network_failure(store):
    checker = AsyncMock(return_value={"consistent": False, "reason": "与固定人设冲突"})
    _, proposal = await candidate(store, check=checker)
    assert proposal["check_result"]["retryable"] is False
    await age(store, proposal)
    with pytest.raises(ValueError, match="暂时检查失败"):
        await store.personas.retry_check(OWNER, proposal["id"], consistent)
    _, same = await candidate(store, check=checker)
    assert same["id"] == proposal["id"] and checker.await_count == 1


async def test_retry_rejects_other_users_and_changed_persona(store):
    _, proposal = await candidate(store, check=failed)
    await age(store, proposal)
    checker = AsyncMock()
    with pytest.raises(ValueError, match="主人"):
        await store.personas.retry_check("qq:other", proposal["id"], checker)
    profile = await store.personas.profile()
    await store.personas.save_profile(
        {**profile["profile"], "name": "新版名字"}, profile["version"]
    )
    with pytest.raises(ValueError, match="人设或习惯已变化"):
        await store.personas.retry_check(OWNER, proposal["id"], checker)
    checker.assert_not_called()


async def test_concurrent_retries_claim_only_one_check(store):
    _, proposal = await candidate(store, check=failed)
    await age(store, proposal)
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        started.set()
        await finish.wait()
        return await consistent(*args)

    first = asyncio.create_task(store.personas.retry_check(OWNER, proposal["id"], slow))
    try:
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(ValueError, match="暂时检查失败"):
            await store.personas.retry_check(OWNER, proposal["id"], consistent)
    finally:
        finish.set()
        result = await first
    assert result["status"] == "pending" and result["check_result"]["attempts"] == 2


async def test_cancelled_retry_leaves_recoverable_failure_not_checking(store):
    _, proposal = await candidate(store, check=failed)
    await age(store, proposal)
    started = asyncio.Event()

    async def slow(*args):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store.personas.retry_check(OWNER, proposal["id"], slow))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    latest = (await store.personas.proposals(OWNER))[0]
    assert latest["status"] == "blocked" and latest["check_result"]["retryable"]
    assert latest["check_result"]["attempts"] == 2


async def test_concurrent_identical_submissions_only_run_one_check(store):
    source = await evidence(store)
    checker = AsyncMock(side_effect=consistent)

    async def submit():
        return await store.personas.propose(
            OWNER,
            "L1",
            "speaking_habit",
            "简短交流",
            "表达习惯候选",
            [{"message_id": source["id"], "quote": source["content"]}],
            checker,
        )

    results = await asyncio.gather(*(submit() for _ in range(5)))
    assert len({p["id"] for p in results}) == 1
    assert checker.await_count == 1
    assert (await store.personas.proposals(OWNER))[0]["status"] == "pending"


async def test_persona_change_during_retry_makes_result_stale(store):
    _, proposal = await candidate(store, check=failed)
    await age(store, proposal)

    async def change_during_check(*args):
        current = await store.personas.profile()
        await store.personas.save_profile(
            {**current["profile"], "name": "检查期间新设定"}, current["version"]
        )
        return await consistent(*args)

    result = await store.personas.retry_check(OWNER, proposal["id"], change_during_check)
    assert result["status"] == "stale"
    assert not await store.personas.active_traits(OWNER)
