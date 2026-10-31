"""PostgreSQL memory lifecycle independent of the AstrBot runtime."""

import json
import math
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .persona import PersonaStore
from .settings import DEFAULT_SETTINGS, validate_settings

TZ = ZoneInfo("Asia/Shanghai")


def level(mentioned_at, now):
    """Calculate activity using elapsed 48-hour intervals."""
    return max(0, 10 - max(0, math.floor((now - mentioned_at).total_seconds() / 172800)))


def tokens(text):
    """Use a deliberately conservative UTF-8 byte upper estimate."""
    return len(text.encode("utf-8"))


def terms(text):
    """Extract Latin words and Chinese bigrams for bounded local retrieval."""
    out = set(re.findall(r"[a-zA-Z0-9_]{2,}", text.lower()))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        out.update(phrase[i : i + 2] for i in range(len(phrase) - 1))
    return out


class MemoryStore:
    def __init__(self, dsn, embedder=None):
        self.embedder = embedder
        self.pool = AsyncConnectionPool(
            dsn,
            open=False,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            timeout=8,
        )
        self.personas = PersonaStore(self.pool)

    async def open(self):
        await self.pool.open(wait=True, timeout=10)
        async with self.pool.connection() as conn:
            await conn.execute(Path(__file__).with_name("schema.sql").read_text("utf-8"))
            await self.personas.migrate(conn)
            await conn.execute(
                "UPDATE bot_settings SET payload=payload-'bot_name'-'persona' WHERE id=true"
            )

    async def close(self):
        await self.pool.close()

    async def get_settings(self):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT payload FROM bot_settings WHERE id=true")
            ).fetchone()
        values = {**DEFAULT_SETTINGS, **(row["payload"] if row else {})}
        profile = (await self.personas.profile())["profile"]
        values.update(bot_name=profile["name"], persona=profile["personality"])
        return values

    async def save_settings(self, payload):
        values = validate_settings(payload)
        async with self.pool.connection() as conn:
            profile = await (
                await conn.execute(
                    "SELECT payload,version FROM persona_profile WHERE id=true FOR UPDATE"
                )
            ).fetchone()
            updated = {
                **profile["payload"],
                "name": values["bot_name"],
                "personality": values["persona"],
            }
            await self.personas._write_profile(conn, updated, profile["version"])
            await conn.execute(
                "INSERT INTO bot_settings(id,payload) VALUES (true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()",
                (Jsonb({k: values[k] for k in ("memory_budget", "history_budget")}),),
            )
        return values

    async def save_budgets(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"memory_budget", "history_budget"}:
            raise ValueError("预算字段不完整")
        values = validate_settings({**DEFAULT_SETTINGS, **payload})
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO bot_settings(id,payload) VALUES (true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()",
                (Jsonb({key: values[key] for key in payload}),),
            )
        return payload

    async def backfill_vectors(self):
        if not self.embedder:
            return 0
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id,content FROM memories WHERE status<>'invalid' AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s) ORDER BY id LIMIT 100",
                    (self.embedder.model_id,),
                )
            ).fetchall()
        if rows:
            vectors = await self.embedder.encode([r["content"] for r in rows])
            async with self.pool.connection() as conn:
                for row, vector in zip(rows, vectors, strict=True):
                    await conn.execute(
                        "UPDATE memories SET embedding=%s::public.vector,embedding_model=%s WHERE id=%s",
                        (json.dumps(vector), self.embedder.model_id, row["id"]),
                    )
        return len(rows)

    async def record(self, platform, account, conversation, event_id, role, content, at=None):
        person = f"{platform}:{account}"
        at = at or datetime.now(UTC)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO persons(id) VALUES (%s) ON CONFLICT DO NOTHING", (person,)
            )
            await conn.execute(
                "INSERT INTO platform_accounts VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (platform, account, person),
            )
            person = (
                await (
                    await conn.execute(
                        "SELECT person_id FROM platform_accounts WHERE platform=%s AND account_id=%s",
                        (platform, account),
                    )
                ).fetchone()
            )["person_id"]
            row = await (
                await conn.execute(
                    """INSERT INTO messages(person_id,conversation_id,event_id,role,content,created_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(conversation_id,event_id,role)
                DO UPDATE SET event_id=EXCLUDED.event_id RETURNING *""",
                    (person, conversation, event_id, role, content, at),
                )
            ).fetchone()
        return row

    async def archive(self, now=None):
        now = now or datetime.now(UTC)
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """UPDATE memories SET status='archived',updated_at=%s
                WHERE kind='ordinary' AND status='active' AND last_user_mentioned_at <= %s RETURNING id,person_id""",
                    (now, now - timedelta(days=8)),
                )
            ).fetchall()
            for row in rows:
                await conn.execute(
                    "INSERT INTO memory_events(person_id,memory_id,action) VALUES (%s,%s,'archive')",
                    (row["person_id"], row["id"]),
                )
        return len(rows)

    async def memories(self, person, include_invalid=False):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    """SELECT m.*, (SELECT json_agg(json_build_object('message_id',s.message_id,'quote',s.quote))
                FROM memory_sources s WHERE s.memory_id=m.id) AS sources FROM memories m WHERE person_id=%s
                AND (%s OR status <> 'invalid') ORDER BY last_user_mentioned_at DESC,id DESC""",
                    (person, include_invalid),
                )
            ).fetchall()

    async def confirm(self, person, memory_id, message_id, quote):
        """Refresh only with an exact quotation from the current user's message."""
        async with self.pool.connection() as conn:
            message = await (
                await conn.execute(
                    "SELECT * FROM messages WHERE id=%s AND person_id=%s AND role='user'",
                    (message_id, person),
                )
            ).fetchone()
            mem = await (
                await conn.execute(
                    "SELECT * FROM memories WHERE id=%s AND person_id=%s AND status <> 'invalid' FOR UPDATE",
                    (memory_id, person),
                )
            ).fetchone()
            if not message or not mem or not quote.strip() or quote not in message["content"]:
                raise ValueError("A matching user-message quotation is required")
            if message["created_at"] < mem["last_user_mentioned_at"]:
                return False
            await conn.execute(
                "UPDATE memories SET status='active',last_user_mentioned_at=%s,updated_at=now() WHERE id=%s",
                (message["created_at"], memory_id),
            )
            await conn.execute(
                "INSERT INTO memory_sources VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (memory_id, message_id, quote),
            )
            await conn.execute(
                "INSERT INTO memory_events(person_id,memory_id,action,message_id) VALUES (%s,%s,'user_confirm',%s)",
                (person, memory_id, message_id),
            )
        return True

    async def recent(self, conversation, before_id, budget=4000):
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT role,content FROM messages WHERE conversation_id=%s
                AND id < %s ORDER BY id DESC LIMIT 100""",
                    (conversation, before_id),
                )
            ).fetchall()
        selected, cost = [], 0
        for row in rows:
            size = tokens(row["content"]) + 32
            if cost + size > budget:
                break
            selected.append(row)
            cost += size
        selected.reverse()
        while selected and selected[0]["role"] != "user":
            selected.pop(0)
        return selected

    async def context(self, person, message, budget=2000, history_budget=4000):
        await self.archive()
        rows = await self.memories(person)
        query_terms = terms(message["content"])

        def scored(r):
            return (len(query_terms & terms(r["content"])), r["last_user_mentioned_at"], r["id"])

        core = sorted([r for r in rows if r["kind"] == "core"], key=scored, reverse=True)
        active = sorted(
            [r for r in rows if r["kind"] == "ordinary" and r["status"] == "active"],
            key=scored,
            reverse=True,
        )
        archived = [r for r in rows if r["kind"] == "ordinary" and r["status"] == "archived"]
        random_mem = random.choice(archived) if archived else None
        selected, blocks, cost = [], [], 0
        # Random sampling is independent of lexical retrieval and never refreshes activity.
        categories = [
            (
                "偶然回忆：无关就忽略，不必说起",
                [random_mem] if random_mem else [],
                int(budget * 0.2),
            ),
            ("核心事实", core, int(budget * 0.25)),
            ("活跃记忆", active, budget - int(budget * 0.2) - int(budget * 0.25)),
        ]
        for label, candidates, cap in categories:
            used = 0
            for row in candidates:
                line = f"[{row['id']}] {row['content']}"
                size = tokens(line) + tokens(label) + 8
                if used + size > cap or cost + size > budget:
                    continue
                blocks.append(f"{label} {line}")
                selected.append(row["id"])
                cost += size
                used += size
        history = await self.recent(message["conversation_id"], message["id"], history_budget)
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO context_runs(person_id,message_id,injected_ids,random_memory_id,
                estimated_memory_tokens,estimated_history_tokens) VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    person,
                    message["id"],
                    Jsonb(selected),
                    random_mem["id"] if random_mem else None,
                    cost,
                    sum(tokens(m["content"]) + 32 for m in history),
                ),
            )
        return "\n".join(blocks), history

    async def search(self, person, query, limit=8, budget=1600):
        await self.archive()
        if self.embedder:
            vector = (await self.embedder.encode([query]))[0]
            async with self.pool.connection() as conn:
                candidates = await (
                    await conn.execute(
                        """SELECT id,content,status,
                    1-(embedding OPERATOR(public.<=>) %s::public.vector) AS similarity
                    FROM memories WHERE person_id=%s AND status<>'invalid' AND embedding_model=%s
                    AND embedding IS NOT NULL ORDER BY embedding OPERATOR(public.<=>) %s::public.vector LIMIT %s""",
                        (
                            json.dumps(vector),
                            person,
                            self.embedder.model_id,
                            json.dumps(vector),
                            limit,
                        ),
                    )
                ).fetchall()
            results, cost = [], 0
            for row in candidates:
                if row["similarity"] < 0.4:
                    continue
                size = tokens(json.dumps(row, ensure_ascii=False)) + 2
                if cost + size + 2 <= budget:
                    results.append(row)
                    cost += size
            return results
        rows = await self.memories(person)
        query_terms = terms(query)
        ranked = sorted(rows, key=lambda r: len(query_terms & terms(r["content"])), reverse=True)
        results, cost = [], 0
        for row in ranked:
            if not query_terms & terms(row["content"]):
                continue
            item = {"id": row["id"], "content": row["content"], "status": row["status"]}
            size = tokens(json.dumps(item, ensure_ascii=False)) + 2
            if cost + size + 2 > budget:
                continue
            results.append(item)
            cost += size
            if len(results) >= limit:
                break
        return results

    async def invalidate(self, person, memory_id, reason):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE memories SET status='invalid',updated_at=now() WHERE id=%s AND person_id=%s RETURNING id",
                    (memory_id, person),
                )
            ).fetchone()
            if not row:
                raise ValueError("Memory not found")
            await conn.execute(
                "INSERT INTO memory_events(person_id,memory_id,action,details) VALUES (%s,%s,'manual_invalidate',%s)",
                (person, memory_id, Jsonb({"reason": reason})),
            )

    async def pending_days(self):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute("""SELECT DISTINCT m.person_id,(m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS day
                FROM messages m LEFT JOIN daily_jobs j ON j.person_id=m.person_id
                AND j.day=(m.created_at AT TIME ZONE 'Asia/Shanghai')::date
                WHERE m.role='user' AND (m.created_at AT TIME ZONE 'Asia/Shanghai')::date < (now() AT TIME ZONE 'Asia/Shanghai')::date
                AND (j.person_id IS NULL OR (j.status <> 'complete' AND j.attempts < 3)
                OR (j.status='complete' AND m.id > j.checkpoint)) ORDER BY day LIMIT 14""")
            ).fetchall()

    async def consolidate(self, person, day, extract):
        """Apply one bounded daily job atomically, with persisted retry and source checks."""
        async with self.pool.connection() as lock_conn:
            lock_key = f"companion:{person}"
            locked = (
                await (
                    await lock_conn.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok",
                        (lock_key,),
                    )
                ).fetchone()
            )["ok"]
            await lock_conn.commit()
            if not locked:
                return {"status": "busy"}
            try:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO daily_jobs(person_id,day) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (person, day),
                    )
                    job = await (
                        await conn.execute(
                            "SELECT * FROM daily_jobs WHERE person_id=%s AND day=%s",
                            (person, day),
                        )
                    ).fetchone()
                    if job["attempts"] >= 3 and job["status"] != "complete":
                        return {"status": "retry_limit"}
                    rows = await (
                        await conn.execute(
                            """SELECT * FROM messages WHERE person_id=%s
                        AND (created_at AT TIME ZONE 'Asia/Shanghai')::date=%s AND id>%s ORDER BY id""",
                            (person, day, job["checkpoint"]),
                        )
                    ).fetchall()
                    if not rows:
                        return {"status": "up_to_date"}
                    await conn.execute(
                        "UPDATE daily_jobs SET status='running',attempts=attempts+1,updated_at=now() WHERE person_id=%s AND day=%s",
                        (person, day),
                    )
                # Batches cap the extraction input. Oversized individual messages remain stored in full.
                batches, batch, size = [], [], 0
                for msg in rows:
                    if len(msg["content"].encode("utf-8")) > 24000:
                        raise ValueError(
                            "Daily input contains an oversized message; split/review it before retrying"
                        )
                    if size + tokens(msg["content"]) > 24000 and batch:
                        batches.append(batch)
                        batch, size = [], 0
                    batch.append(msg)
                    size += tokens(msg["content"])
                if batch:
                    batches.append(batch)
                if len(batches) > 8:
                    raise ValueError("Daily extraction exceeded the eight-batch limit")
                existing = await self.memories(person)
                # Only bounded relevant existing memories are sent for duplicate/correction matching.
                day_terms = terms(" ".join(m["content"] for m in rows if m["role"] == "user"))
                existing.sort(key=lambda m: len(day_terms & terms(m["content"])), reverse=True)
                known, used = [], 0
                for m in existing:
                    item = {k: m[k] for k in ("id", "content", "kind")}
                    n = tokens(json.dumps(item, ensure_ascii=False))
                    if used + n <= 6000:
                        known.append(item)
                        used += n
                edits = []
                for batch in batches:
                    result = await extract(batch, known)
                    if not isinstance(result, list) or len(result) > 30:
                        raise ValueError("Invalid daily extraction output")
                    edits.extend(result)
                source_map = {m["id"]: m for m in rows if m["role"] == "user"}
                known_ids = {m["id"] for m in known}
                # Validate the entire output before the transaction changes any memories.
                for edit in edits:
                    source = source_map.get(edit.get("message_id"))
                    quote = edit.get("quote", "")
                    if not source or not quote.strip() or quote not in source["content"]:
                        raise ValueError("Extraction quote must match a user message exactly")
                    if (
                        edit.get("kind") not in ("ordinary", "core")
                        or not 1 <= len(edit.get("content", "")) <= 240
                    ):
                        raise ValueError("Invalid memory kind or content length")
                    if edit.get("action") not in (
                        "create",
                        "confirm",
                        "replace",
                        "invalidate",
                    ):
                        raise ValueError("Invalid memory action")
                    if edit.get("action") != "create" and edit.get("target_id") not in known_ids:
                        raise ValueError("Unknown target memory")
                changed = []
                vectors = {}
                if self.embedder:
                    contents = list(
                        dict.fromkeys(
                            e["content"] for e in edits if e["action"] in ("create", "replace")
                        )
                    )
                    if contents:
                        vectors = dict(
                            zip(contents, await self.embedder.encode(contents), strict=True)
                        )
                async with self.pool.connection() as conn:
                    for edit in edits:
                        source = source_map[edit["message_id"]]
                        action, target = edit["action"], edit.get("target_id")
                        if action == "confirm":
                            await conn.execute(
                                "UPDATE memories SET last_user_mentioned_at=greatest(last_user_mentioned_at,%s),status='active',updated_at=now() WHERE id=%s AND person_id=%s AND status<>'invalid'",
                                (source["created_at"], target, person),
                            )
                            mid = target
                        elif action == "invalidate":
                            await conn.execute(
                                "UPDATE memories SET status='invalid',updated_at=now() WHERE id=%s AND person_id=%s",
                                (target, person),
                            )
                            mid = target
                        else:
                            duplicate = await (
                                await conn.execute(
                                    "SELECT id FROM memories WHERE person_id=%s AND content=%s AND kind=%s AND status<>'invalid'",
                                    (person, edit["content"], edit["kind"]),
                                )
                            ).fetchone()
                            if duplicate:
                                mid = duplicate["id"]
                                await conn.execute(
                                    "UPDATE memories SET last_user_mentioned_at=greatest(last_user_mentioned_at,%s),status='active',updated_at=now() WHERE id=%s",
                                    (source["created_at"], mid),
                                )
                            else:
                                mid = (
                                    await (
                                        await conn.execute(
                                            "INSERT INTO memories(person_id,content,kind,last_user_mentioned_at) VALUES (%s,%s,%s,%s) RETURNING id",
                                            (
                                                person,
                                                edit["content"],
                                                edit["kind"],
                                                source["created_at"],
                                            ),
                                        )
                                    ).fetchone()
                                )["id"]
                            if action == "replace" and target != mid:
                                await conn.execute(
                                    "UPDATE memories SET status='invalid',superseded_by=%s,updated_at=now() WHERE id=%s AND person_id=%s",
                                    (mid, target, person),
                                )
                        await conn.execute(
                            "INSERT INTO memory_sources VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (mid, source["id"], edit["quote"]),
                        )
                        if edit["content"] in vectors and action in ("create", "replace"):
                            await conn.execute(
                                "UPDATE memories SET embedding=%s::public.vector,embedding_model=%s WHERE id=%s",
                                (json.dumps(vectors[edit["content"]]), self.embedder.model_id, mid),
                            )
                        await conn.execute(
                            "INSERT INTO memory_events(person_id,memory_id,action,message_id) VALUES (%s,%s,%s,%s)",
                            (person, mid, action, source["id"]),
                        )
                        changed.append(mid)
                    await conn.execute(
                        "UPDATE daily_jobs SET status='complete',checkpoint=%s,error=NULL,updated_at=now() WHERE person_id=%s AND day=%s",
                        (max(m["id"] for m in rows), person, day),
                    )
                await self.archive()
                return {
                    "status": "complete",
                    "changed_ids": changed,
                    "batches": len(batches),
                }
            except Exception as exc:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE daily_jobs SET status='failed',error=%s,updated_at=now() WHERE person_id=%s AND day=%s",
                        (type(exc).__name__, person, day),
                    )
                raise
            finally:
                await lock_conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_key,)
                )
                await lock_conn.commit()
