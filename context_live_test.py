"""Isolated real-API continuity, summary and cache replay with public synthetic data."""

import asyncio
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
from openai import AsyncOpenAI
from psycopg import sql
from psycopg.conninfo import make_conninfo

from local_secrets import database_parameters
from plugin.continuity import SUMMARY_PROMPT
from plugin.memory import MemoryStore
from plugin.persona import build_system_prompt
from plugin.settings import DEFAULT_SETTINGS
from plugin.token_counting import tokens
from plugin.usage import UsageRecorder, usage_scope

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")


async def main():
    config = json.loads((ROOT / "local_config.json").read_text("utf-8-sig"))
    key = re.search(
        r"sk-[A-Za-z0-9_-]{12,}", Path(config["deepseek_key_file"]).read_text("utf-8-sig")
    ).group()
    params = database_parameters()
    schema = "test_companion_budget_" + uuid.uuid4().hex
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = {**params, "options": "-csearch_path=" + schema}
    store = MemoryStore(make_conninfo(**scoped))
    await store.open()
    client = AsyncOpenAI(
        api_key=key, base_url="https://api.deepseek.com", timeout=240, max_retries=0
    )
    usage = UsageRecorder(store.pool, store.policy)
    usage.install(SimpleNamespace(client=client))
    try:
        await store.record(
            "validation", "long-context", "isolated", "seed", "user", "今天想继续聊我们的生活计划。"
        )
        filler = "散步时我注意到路边的树叶和天空，想把日常里的小事慢慢讲给你听。我们不用急着下结论，先把眼前的感受说清楚，再看看下次如何安排。"
        values = []
        now = datetime.now(UTC)
        anchors = {
            0: "我把备用钥匙放在紫色陶罐里。请记住我的原话：“你先听我说完，再给建议”。",
            70: "我因为准备搬家、想到要和老朋友分开而难过，并不是对你生气。",
            300: "我们说好下周二晚上聊选电影的事，但是还没有确定片名，请继续留着这个话题。",
            599: "现在最新的话题是阳台上那盆迷迭香，该怎么照顾。",
        }
        for i in range(600):
            user = f"第{i}次交流。" + anchors.get(i, "") + filler * 18
            assistant = "我在听，会先回应你的感受，再一起把还没谈完的事继续聊清楚。"
            for role, content in [("user", user), ("assistant", assistant)]:
                values.append(
                    (
                        "validation:long-context",
                        "isolated",
                        str(i),
                        role,
                        content,
                        now,
                        tokens(content),
                    )
                )
        async with store.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO messages(person_id,conversation_id,event_id,role,content,created_at,token_count) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
        current = await store.record(
            "validation", "long-context", "isolated", "current", "user", "继续聊刚才的话题。"
        )
        raw_tokens = sum(v[-1] + 16 for v in values)
        print(
            "Synthetic full history:",
            len(values),
            "messages,",
            raw_tokens,
            "planned tokens",
            flush=True,
        )

        async def generate(previous, rows):
            with usage_scope("validation_summary", max_output=10000):
                response = await client.chat.completions.create(
                    model="deepseek-flash",
                    messages=[
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "previous": previous,
                                    "messages": [
                                        {k: r[k] for k in ("id", "role", "content", "created_at")}
                                        for r in rows
                                    ],
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                        },
                    ],
                    response_format={"type": "json_object"},
                )
            return json.loads(response.choices[0].message.content)

        history, details = await store.continuity.build(current["person_id"], current, generate)
        print("Compaction:", json.dumps(details, ensure_ascii=False), flush=True)
        assert details["summary_through"] > 0 and details["history_raw_tokens"] < 240000
        prompt = "请只返回JSON，准确回答过去的五件事：key_place（备用钥匙位置）、emotional_reason（搬家时为什么难过、是否对你生气）、open_plan（未决定的周二安排）、quoted_sentence（我关于先听再建议的逐字原话）、latest_topic（最新正在聊的植物）。"
        profile = {
            "name": DEFAULT_SETTINGS["bot_name"],
            "personality": DEFAULT_SETTINGS["persona"],
            "speech_style": "",
            "boundaries": "",
            "worldview": "",
        }
        messages = [
            {
                "role": "system",
                "content": build_system_prompt(profile, [], "历史摘要仅供参考，准确区分用户陈述。"),
            },
            *history,
            {"role": "user", "content": prompt},
        ]
        answers = []
        for attempt in range(2):
            with usage_scope("validation_recall", max_output=3000):
                response = await client.chat.completions.create(
                    model="deepseek-flash",
                    messages=messages,
                    response_format={"type": "json_object"},
                )
            answer = json.loads(response.choices[0].message.content)
            answers.append(answer)
            assert "紫色陶罐" in answer["key_place"], answer
            assert "搬家" in answer["emotional_reason"] and "朋友" in answer["emotional_reason"], (
                answer
            )
            assert re.search(
                r"(不是|并非|并不|没有).{0,6}(生气|气你)|不生.{0,3}气", answer["emotional_reason"]
            ), answer
            assert "周二" in answer["open_plan"] and "电影" in answer["open_plan"], answer
            assert any(word in answer["open_plan"] for word in ("未", "没", "待")), answer
            assert "你先听我说完，再给建议" in answer["quoted_sentence"], answer
            assert "迷迭香" in answer["latest_topic"], answer
            if attempt == 0:
                await asyncio.sleep(3)
        async with store.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT category,input_tokens,output_tokens,cache_hit_tokens,cache_miss_tokens,latency_ms,estimated_usd_low,estimated_usd_high FROM api_usage ORDER BY started_at"
                )
            ).fetchall()
            raw_count = (
                await (await conn.execute("SELECT count(*) AS n FROM messages")).fetchone()
            )["n"]
        assert raw_count == 1202
        evidence = {
            "synthetic_messages": raw_count,
            "raw_tokens": raw_tokens,
            "context": details,
            "answers": answers,
            "usage": rows,
            "checks": {
                "early_fact": True,
                "emotional_continuity": True,
                "unfinished_topic": True,
                "exact_quote": True,
                "latest_topic": True,
                "raw_records_retained": True,
            },
        }
        (ROOT / "runtime/qa/context-live-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
        )
        print("Real-API replay passed: 5/5 checks on both requests; sources retained.", flush=True)
        print("API usage:", json.dumps(rows, default=str), flush=True)
    finally:
        await client.close()
        await store.close()
        assert schema.startswith("test_companion_budget_")
        with psycopg.connect(**params, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
