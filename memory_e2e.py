"""Validate real daily LLM extraction and local pgvector recall with isolated test identities."""

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from local_secrets import database_parameters
from plugin.embeddings import LocalEmbedding
from plugin.memory import TZ, MemoryStore, level

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")


async def main():
    identity = "e2e-" + uuid.uuid4().hex
    person = "validation:" + identity
    store = MemoryStore(
        make_conninfo(**database_parameters()),
        LocalEmbedding(ROOT / "runtime/models/bge-small-zh-v1.5"),
    )
    await store.open()
    try:
        source = await store.record(
            "validation",
            identity,
            identity,
            "old",
            "user",
            "我今天下午去了滨江公园散步。",
            datetime.now(UTC) - timedelta(days=9),
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:6185", trust_env=False, timeout=120
        ) as client:
            result = (
                await client.post(
                    "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/consolidate",
                    json={"person": person, "day": str(source["created_at"].astimezone(TZ).date())},
                )
            ).json()
            assert result.get("status") == "complete" and result["changed_ids"], result
            memories = await store.memories(person)
            assert memories and memories[0]["status"] == "archived", memories
            assert memories[0]["embedding"] is not None
            search = await store.search(person, "以前去哪里走路放松过")
            assert search and "公园" in search[0]["content"], search
            mid = search[0]["id"]
            assert (await store.memories(person))[0]["status"] == "archived"
            current = await store.record(
                "validation", identity, identity, "now", "user", "我上次去了滨江公园散步。"
            )
            await store.confirm(person, mid, current["id"], "去了滨江公园散步")
            revived = (await store.memories(person))[0]
            assert (
                revived["status"] == "active"
                and level(revived["last_user_mentioned_at"], datetime.now(UTC)) == 10
            )
            duplicate = (
                await client.post(
                    "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/consolidate",
                    json={"person": person, "day": str(source["created_at"].astimezone(TZ).date())},
                )
            ).json()
            assert duplicate["status"] == "up_to_date"
            evidence = {
                "daily": result,
                "semantic_recall": search,
                "reactivated_level": 10,
                "duplicate_job": duplicate,
            }
            (ROOT / "runtime/qa/memory-e2e.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
            )
            print(
                "Real daily extraction -> PostgreSQL vector -> archived semantic recall -> Lv.10 reactivation: PASS"
            )
    finally:
        await store.close()
        # Delete only this run's synthetic identity, never the owner's chat records.
        assert person.startswith("validation:e2e-")
        with psycopg.connect(**database_parameters()) as conn:
            conn.execute("DELETE FROM context_runs WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM memory_events WHERE person_id=%s", (person,))
            conn.execute(
                "DELETE FROM memory_sources WHERE memory_id IN (SELECT id FROM memories WHERE person_id=%s)",
                (person,),
            )
            conn.execute("UPDATE memories SET superseded_by=NULL WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM memories WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM daily_jobs WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM messages WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM platform_accounts WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM persons WHERE id=%s", (person,))


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
