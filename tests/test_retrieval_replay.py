import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from plugin.embeddings import LocalEmbedding
from plugin.memory import TZ


async def test_hybrid_recall_archives_long_memory_tail_and_unrelated_query(store):
    store.embedder = LocalEmbedding("runtime/models/bge-small-zh-v1.5")
    texts = [
        "上次我在滨江公园散步，想起外婆，有一点难过。",
        "我一直喜欢喝茉莉花茶，睡前不喝咖啡。",
        "这周准备周二晚上挑一部老电影，还没选好。",
        "我最近整理了家里的各种书本和锅碗瓢盆。" * 80 + "最后我把备用钥匙放在紫色陶罐里。",
    ]
    ids = []
    for i, text in enumerate(texts):
        at = datetime.now(UTC) - timedelta(days=9 if i == 0 else 0)
        message = await store.record("validation", "recall", "recall", str(i), "user", text, at)

        async def extract(rows, known, message=message):
            return [
                {
                    "action": "create",
                    "kind": "ordinary",
                    "content": message["content"],
                    "message_id": message["id"],
                    "quote": message["content"][:20],
                }
            ]

        result = await store.consolidate(message["person_id"], at.astimezone(TZ).date(), extract)
        ids.append(result["changed_ids"][0])
    queries = [
        "以前在哪儿走路放松来着",
        "我通常最爱喝哪种茶",
        "周二那个还没定下来的约定是什么",
        "备用钥匙放在哪个容器",
    ]
    evidence = []
    for expected, query in zip(ids, queries, strict=True):
        result = await store.search(message["person_id"], query)
        found = expected in [row["id"] for row in result[:3]]
        evidence.append(
            {
                "query": query,
                "expected": expected,
                "top3": [row["id"] for row in result[:3]],
                "hit": found,
            }
        )
    irrelevant = await store.search(message["person_id"], "冥王星轨道倾角是多少")
    target = Path("runtime/qa/recall-replay.json")
    target.write_text(
        json.dumps(
            {
                "cases": evidence,
                "recall_at_3": sum(item["hit"] for item in evidence) / len(evidence),
                "irrelevant_returned": len(irrelevant),
                "irrelevant_scores": [r["score"] for r in irrelevant],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    assert all(item["hit"] for item in evidence), evidence
    assert not await store.search(message["person_id"], "你好")
    async with store.pool.connection() as conn:
        count = (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM memory_chunks WHERE memory_id=%s", (ids[-1],)
                )
            ).fetchone()
        )["n"]
    assert count > 1
