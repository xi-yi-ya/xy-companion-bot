from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from local_web import keep_companion_history
from plugin.context_policy import DEFAULT_POLICY
from plugin.continuity import Continuity
from plugin.costs import monthly_scenarios
from plugin.usage import UsageRecorder, cost_range, usage_scope


async def seed_dialog(store, count=120, text="这段交流中我们在认真讨论未完成的旅行计划。"):
    rows = []
    for i in range(count):
        for role, content in [
            ("user", f"第{i}轮：" + text),
            ("assistant", "我记着这个计划，先把你的感受说完。"),
        ]:
            rows.append(
                await store.record("validation", "context", "web:context", str(i), role, content)
            )
    return rows, await store.record(
        "validation", "context", "web:context", "current", "user", "接着聊"
    )


def summary(previous, rows):
    result = {
        k: list((previous or {}).get(k, []))
        for k in ("relationships", "facts", "emotional_arc", "open_threads", "episodes", "quotes")
    }
    user = next(r for r in rows if r["role"] == "user")
    if not result["quotes"]:
        result["quotes"] = [{"message_id": user["id"], "quote": user["content"][:12]}]
        result["open_threads"] = [
            {"text": "旅行计划尚未确定，先讨论感受", "message_ids": [user["id"]]}
        ]
    return result


async def small_policy(store, **changes):
    policy = {
        **DEFAULT_POLICY,
        "history_tiers": [500, 1500, 3000],
        "history_max": 3000,
        "compact_at": 2600,
        "retain_recent": 1700,
        "summary_input": 5000,
        "summary_budget": 600,
        "working_input": 24000,
        "tool_reserve": 1000,
        "safety_reserve": 1000,
        **changes,
    }

    async def get():
        return policy

    store.policy.get = get
    return policy


async def test_more_than_100_messages_and_20_turns_remain_complete(store):
    rows, current = await seed_dialog(store)
    history, details = await store.continuity.build(current["person_id"], current, None)
    assert len(history) == len(rows) == 240
    assert "第0轮" in history[0]["content"] and "第119轮" in history[-2]["content"]
    assert all(m["role"] == ("user" if i % 2 == 0 else "assistant") for i, m in enumerate(history))
    assert not details["silent_truncation"]


async def test_pressure_summary_keeps_recent_complete_turns_and_all_raw_sources(store):
    rows, current = await seed_dialog(store)
    await small_policy(store)

    async def generate(previous, batch):
        return summary(previous, batch)

    history, details = await store.continuity.build(current["person_id"], current, generate)
    assert history[0]["role"] == "system" and "旅行计划尚未确定" in history[0]["content"]
    assert "第119轮" in history[-2]["content"]
    assert details["history_raw_tokens"] < 3000 and details["summary_through"] > 0
    async with store.pool.connection() as conn:
        count = await (await conn.execute("SELECT count(*) AS n FROM messages")).fetchone()
    assert count["n"] == len(rows) + 1
    assert await store.search_history(current["person_id"], "第0轮")


async def test_failed_summary_never_advances_or_discards_unsummarized_history(store):
    rows, current = await seed_dialog(store)
    await small_policy(store)

    async def fail(*args):
        raise RuntimeError("simulated model outage")

    history, details = await store.continuity.build(current["person_id"], current, fail)
    assert len(history) == len(rows) and "第0轮" in history[0]["content"]
    assert await store.continuity.latest(current["person_id"]) is None
    assert not details["silent_truncation"]


async def test_failed_background_summary_backs_off_while_preserving_full_history(store):
    rows, current = await seed_dialog(store)
    await small_policy(store)
    calls = []

    async def fail(*args):
        calls.append(True)
        raise RuntimeError("temporary outage")

    await store.continuity.build(current["person_id"], current, fail)
    history, details = await store.continuity.build(current["person_id"], current, fail)
    assert len(calls) == 1 and len(history) == len(rows)
    assert details["summary_error"] == "RuntimeError" and details["summary_retry_in_seconds"] > 0


def test_summary_rejects_invented_quotes_and_foreign_sources():
    value = summary(None, [{"id": 1, "role": "user", "content": "先听我说完"}])
    value["quotes"][0]["quote"] = "永远不存在的说法"
    with pytest.raises(ValueError):
        Continuity.validate(value, {1: {"content": "先听我说完"}})
    value["quotes"][0] = {"message_id": 99, "quote": "先听我说完"}
    with pytest.raises(ValueError):
        Continuity.validate(value, {1: {"content": "先听我说完"}})


async def test_short_reply_does_not_shrink_an_active_deep_conversation(store):
    _, current = await seed_dialog(store, 50)
    await small_policy(store, history_max=10000, compact_at=9500)
    current["content"] = "嗯"
    history, details = await store.continuity.build(current["person_id"], current, None)
    assert len(history) == 100 and not details["reentry_compaction"]


async def test_raw_history_ranks_old_exact_quotes_before_recent_generic_matches(store):
    old = await store.record(
        "validation", "context", "raw", "old", "user", "我想把备用钥匙放在紫色陶罐里"
    )
    for index in range(135):
        await store.record(
            "validation", "context", "raw", str(index), "user", "我想聊聊今天的天气和晚饭"
        )
    rows = await store.search_history(old["person_id"], "我想 备用钥匙 紫色陶罐")
    assert rows and rows[0]["id"] == old["id"]
    assert "紫色陶罐" in rows[0]["content"]


async def test_long_daily_message_is_split_with_original_quote_validation(store):
    await small_policy(store, daily_input=4096, daily_known=512)
    content = "今天我们一起讨论旅行路线和见面安排，之后还要继续确定。" * 1500
    msg = await store.record("validation", "long", "long", "one", "user", content)
    calls = []

    async def extract(rows, known):
        calls.append(rows)
        row = next(r for r in rows if r["role"] == "user")
        return [
            {
                "action": "create",
                "kind": "ordinary",
                "content": "旅行见面安排仍待确定",
                "message_id": row["id"],
                "quote": row["content"][:10],
            }
        ]

    result = await store.consolidate(
        msg["person_id"],
        msg["created_at"].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).date(),
        extract,
    )
    assert result["status"] == "complete" and len(calls) > 8
    assert len(await store.memories(msg["person_id"])) == 1
    assert all(row["id"] == msg["id"] for batch in calls for row in batch)


async def test_native_compressor_does_not_drop_plugin_managed_history():
    class Manager:
        async def process(self, messages, *args, **kwargs):
            return []

    keep_companion_history(Manager)
    history = [
        SimpleNamespace(role="system", content="<L0_FIXED_PERSONA>stable</L0_FIXED_PERSONA>")
    ] + [SimpleNamespace(role="user", content=str(i)) for i in range(150)]
    assert await Manager().process(history) is history
    assert await Manager().process([SimpleNamespace(role="user", content="other app")]) == []


async def test_real_usage_fields_and_stream_are_counted_once_per_call(store):
    captured = []

    class Stream:
        async def close(self):
            pass

        async def __aiter__(self):
            for text, reason, usage in [
                ("", None, None),
                ("好", "stop", None),
                (
                    None,
                    None,
                    {
                        "prompt_tokens": 100,
                        "completion_tokens": 3,
                        "prompt_cache_hit_tokens": 80,
                        "prompt_cache_miss_tokens": 20,
                    },
                ),
            ]:
                choice = SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=reason)
                yield SimpleNamespace(
                    model="deepseek-flash",
                    id="api-test",
                    choices=[choice] if usage is None else [],
                    usage=usage,
                )

    async def create(**kwargs):
        captured.append(kwargs)
        if kwargs.get("stream"):
            return Stream()
        return SimpleNamespace(
            model="deepseek-flash",
            id="api-test-two",
            choices=[SimpleNamespace(finish_reason="stop")],
            usage={
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 30,
                "prompt_cache_miss_tokens": 20,
            },
        )

    resource = SimpleNamespace(create=create)
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=resource)))
    recorder = UsageRecorder(store.pool, store.policy)
    recorder.install(provider)
    recorder.install(provider)
    with usage_scope("chat"):
        await resource.create(
            messages=[{"role": "user", "content": "你好"}], extra_body={"max_tokens": 4096}
        )
        stream = await resource.create(messages=[{"role": "user", "content": "继续"}], stream=True)
        async for _ in stream:
            pass
    assert captured[0]["max_tokens"] == 12000 and "max_tokens" not in captured[0]["extra_body"]
    async with store.pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM api_usage ORDER BY started_at")).fetchall()
    assert len(rows) == 2 and sum(r["cache_hit_tokens"] for r in rows) == 110
    assert rows[1]["finish_reason"] == "stop" and rows[1]["output_tokens"] == 3
    metrics = await recorder.metrics()
    assert metrics["monthly"][0]["input_tokens"] == 150


def test_price_scenarios_keep_output_and_background_jobs_separate():
    at = datetime(2026, 11, 1, 2, tzinfo=UTC)
    low, high = cost_range(1000000, 0, 0, at)
    assert low == high == 0.003
    scenario = monthly_scenarios()["scenarios"][0]
    assert scenario["chat_peak_usd"] == 28.8
    assert scenario["total_peak_usd"] > scenario["chat_peak_usd"]
    assert monthly_scenarios()["continuous_scenarios"][0]["chat_peak_usd"] == 181.8


async def test_physical_context_guard_rejects_without_network_or_history_truncation(store):
    policy = {**DEFAULT_POLICY, "model_context": 400, "reply_output": 20, "safety_reserve": 20}

    async def get():
        return policy

    async def never(**kwargs):
        raise AssertionError("Oversized input must not reach the network")

    store.policy.get = get
    resource = SimpleNamespace(create=never)
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=resource)))
    recorder = UsageRecorder(store.pool, store.policy)
    recorder.install(provider)
    with pytest.raises(ValueError, match="未静默截断"):
        await resource.create(messages=[{"role": "user", "content": "完整的长话题" * 1000}])
    metrics = await recorder.metrics()
    assert metrics["monthly"][0]["context_rejections"] == 1
    assert metrics["monthly"][0]["missing_usage"] == 1


async def test_provider_error_cannot_silently_pop_older_history(store):
    async def create(**kwargs):
        raise RuntimeError("context_length_exceeded")

    async def fallback(exc, history):
        history.pop(0)

    resource = SimpleNamespace(create=create)
    provider = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(completions=resource)),
        _handle_api_error=fallback,
    )
    recorder = UsageRecorder(store.pool, store.policy)
    recorder.install(provider)
    recorder.install(provider)
    history = ["earlier important exchange", "recent exchange"]
    with pytest.raises(RuntimeError) as failed:
        await resource.create(messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(ValueError, match="未裁剪"):
        await provider._handle_api_error(failed.value, history)
    assert len(history) == 2
    metrics = await recorder.metrics()
    assert metrics["monthly"][0]["context_rejections"] == 1


async def test_daily_resume_keeps_committed_prefix_and_corrects_new_memory_ids(store):
    await small_policy(store, daily_input=4096, daily_known=512)
    messages = []
    for i in range(3):
        messages.append(
            await store.record(
                "validation",
                "daily-resume",
                "daily",
                "s" + str(i),
                "user",
                f"第{i}天计划" + ("我想聊聊散步计划和接下来的安排。" * 95),
            )
        )
    seen = []

    async def first(rows, known):
        seen.append(rows[0]["id"])
        if len(seen) == 2:
            raise RuntimeError("transient service error")
        row = rows[0]
        return [
            {
                "action": "create",
                "kind": "ordinary",
                "content": "准备继续讨论散步",
                "message_id": row["id"],
                "quote": row["content"][:6],
            }
        ]

    day = messages[0]["created_at"].date()
    from plugin.memory import TZ

    day = messages[0]["created_at"].astimezone(TZ).date()
    with pytest.raises(RuntimeError):
        await store.consolidate(messages[0]["person_id"], day, first)
    assert len(await store.memories(messages[0]["person_id"])) == 1
    resumed = []

    async def finish(rows, known):
        resumed.append(rows[0]["id"])
        return []

    result = await store.consolidate(messages[0]["person_id"], day, finish)
    assert result["status"] == "complete" and messages[0]["id"] not in resumed
