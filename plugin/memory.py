"""PostgreSQL memory lifecycle independent of the AstrBot runtime."""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .context_policy import PolicyStore
from .continuity import Continuity
from .persona import PersonaStore
from .settings import DEFAULT_SETTINGS, validate_settings
from .token_counting import split_text, tokens

TZ = ZoneInfo("Asia/Shanghai")


def level(mentioned_at, now):
    """Calculate activity using elapsed 48-hour intervals."""
    return max(0, 10 - max(0, math.floor((now - mentioned_at).total_seconds() / 172800)))


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
        self.policy = PolicyStore(self.pool)
        self.continuity = Continuity(self.pool, self.policy)

    async def open(self):
        await self.pool.open(wait=True, timeout=10)
        async with self.pool.connection() as conn:
            await conn.execute(Path(__file__).with_name("schema.sql").read_text("utf-8"))
            await self.personas.migrate(conn)
            old = await (
                await conn.execute("SELECT id FROM context_policy WHERE id=true")
            ).fetchone()
            await self.policy.migrate(conn)
            if not old:
                await conn.execute(
                    "INSERT INTO bot_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=bot_settings.payload || EXCLUDED.payload",
                    (Jsonb({"memory_budget": 30000, "history_budget": 240000}),),
                )
            await conn.execute(
                "UPDATE bot_settings SET payload=payload-'bot_name'-'persona' WHERE id=true"
            )

    async def close(self):
        await self.continuity.close()
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
                    "SELECT id,content FROM memories WHERE status<>'invalid' AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s OR NOT EXISTS(SELECT 1 FROM memory_chunks WHERE memory_id=memories.id AND embedding_model=%s)) ORDER BY id LIMIT 100",
                    (self.embedder.model_id, self.embedder.model_id),
                )
            ).fetchall()
        if rows:
            prepared = await self.prepare_chunks([r["content"] for r in rows])
            async with self.pool.connection() as conn:
                for row in rows:
                    vector = prepared[row["content"]][0][1]
                    await conn.execute(
                        "UPDATE memories SET embedding=%s::public.vector,embedding_model=%s WHERE id=%s",
                        (json.dumps(vector), self.embedder.model_id, row["id"]),
                    )
                    await self.write_chunks(conn, row["id"], prepared[row["content"]])
        return len(rows)

    async def prepare_chunks(self, contents):
        pairs = [
            (text, text[offset : offset + 360])
            for text in contents
            for offset in range(0, len(text), 300)
        ]
        encoded = await self.embedder.encode([part for _, part in pairs])
        values = {text: [] for text in contents}
        for (text, part), vector in zip(pairs, encoded, strict=True):
            values[text].append((part, vector))
        return values

    async def write_chunks(self, conn, memory_id, chunks):
        await conn.execute("DELETE FROM memory_chunks WHERE memory_id=%s", (memory_id,))
        for i, (text, vector) in enumerate(chunks):
            await conn.execute(
                "INSERT INTO memory_chunks(memory_id,chunk_no,content,embedding,embedding_model) VALUES(%s,%s,%s,%s::public.vector,%s)",
                (memory_id, i, text, json.dumps(vector), self.embedder.model_id),
            )

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
                    """INSERT INTO messages(person_id,conversation_id,event_id,role,content,created_at,token_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(conversation_id,event_id,role)
                DO UPDATE SET event_id=EXCLUDED.event_id RETURNING *""",
                    (person, conversation, event_id, role, content, at, tokens(content)),
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

    async def recent(self, conversation, before_id, budget=240000):
        async with self.pool.connection() as conn:
            owner = await (
                await conn.execute(
                    "SELECT person_id FROM messages WHERE conversation_id=%s LIMIT 1",
                    (conversation,),
                )
            ).fetchone()
        if not owner:
            return []
        rows = await self.continuity.rows(owner["person_id"], before_id)
        from .continuity import complete_turns

        selected = []
        used = 0
        for turn in reversed(complete_turns(rows)):
            cost = self.continuity.cost(turn)
            if selected and used + cost > budget:
                break
            selected[0:0] = turn
            used += cost
        return [{"role": r["role"], "content": r["content"]} for r in selected]

    async def candidates(self, person, query, limit=240):
        vector = (await self.embedder.encode([query]))[0] if self.embedder else None
        fields = "id,content,kind,status,last_user_mentioned_at,importance"
        async with self.pool.connection() as conn:
            if vector is not None:
                data = await (
                    await conn.execute(
                        f"""SELECT {fields},GREATEST(CASE WHEN embedding IS NULL THEN 0 ELSE 1-(embedding OPERATOR(public.<=>) %s::public.vector) END,
                        COALESCE((SELECT max(1-(chunk.embedding OPERATOR(public.<=>) %s::public.vector)) FROM memory_chunks chunk WHERE chunk.memory_id=memories.id AND chunk.embedding_model=%s),0)) AS similarity
                    FROM memories WHERE person_id=%s AND status<>'invalid' ORDER BY similarity DESC LIMIT %s""",
                        (
                            json.dumps(vector),
                            json.dumps(vector),
                            self.embedder.model_id,
                            person,
                            limit,
                        ),
                    )
                ).fetchall()
            else:
                data = await (
                    await conn.execute(
                        f"SELECT {fields},0 AS similarity FROM memories WHERE person_id=%s AND status<>'invalid' ORDER BY last_user_mentioned_at DESC LIMIT %s",
                        (person, limit),
                    )
                ).fetchall()
            baseline = await (
                await conn.execute(
                    f"SELECT {fields},0 AS similarity FROM memories WHERE person_id=%s AND status<>'invalid' AND kind='core' ORDER BY importance DESC,last_user_mentioned_at DESC LIMIT 80",
                    (person,),
                )
            ).fetchall()
        indexed = {r["id"]: r for r in data}
        for row in baseline:
            indexed.setdefault(row["id"], row)
        query_terms = terms(query)
        generic = bool(
            re.fullmatch(
                r"(你好|您好|早上好|晚上好|早安|晚安|嗯|好的|好|哈哈|谢谢)[！!。～~\s]*", query
            )
        )
        now = datetime.now(UTC)
        ranked = []
        for row in indexed.values():
            lexical = min(
                1, len(query_terms & terms(row["content"])) / max(1, min(6, len(query_terms)))
            )
            similarity = float(row.get("similarity") or 0)
            importance = float(row.get("importance") or 0.5)
            pinned = row["kind"] == "core" and importance >= 0.85
            if not pinned and (generic or (lexical == 0 and similarity < 0.43)):
                continue
            age = max(0, (now - row["last_user_mentioned_at"]).total_seconds() / 86400)
            score = (
                0.62 * similarity + 0.22 * lexical + 0.1 * importance + 0.06 * math.exp(-age / 30)
            )
            ranked.append({**row, "score": score, "lexical": lexical})
        return sorted(ranked, key=lambda r: (r["score"], r["id"]), reverse=True)

    async def context(self, person, message, budget=None, history_budget=None, summarize=None):
        await self.archive()
        policy = await self.policy.get()
        budget = policy["memory_budget"] if budget is None else budget
        candidates = await self.candidates(person, message["content"])
        async with self.pool.connection() as conn:
            random_mem = await (
                await conn.execute(
                    "SELECT id,content,kind,status FROM memories WHERE person_id=%s AND status='archived' AND kind='ordinary' ORDER BY random() LIMIT 1",
                    (person,),
                )
            ).fetchone()
        categories = [
            (
                "偶然回忆：无关就忽略，不必说起",
                [random_mem] if random_mem else [],
                policy["random_budget"],
            ),
            ("核心事实", [r for r in candidates if r["kind"] == "core"], policy["core_budget"]),
            (
                "活跃记忆",
                [r for r in candidates if r["kind"] == "ordinary" and r["status"] == "active"],
                policy["active_budget"],
            ),
            (
                "相关旧事",
                [r for r in candidates if r["status"] == "archived"],
                policy["archive_recall_budget"],
            ),
        ]
        selected = []
        excerpts = []
        blocks = []
        cost = 0
        for label, rows, cap in categories:
            used = 0
            for row in rows:
                if row["id"] in selected:
                    continue
                line = f"{label} [{row['id']}] {row['content']}"
                size = tokens(line) + 8
                remaining = min(cap - used, budget - cost)
                if row is random_mem and size > remaining and remaining >= 100:
                    prefix = f"{label} [{row['id']}] （节选，可检索全文）"
                    fragment = split_text(row["content"], max(1, remaining - tokens(prefix) - 16))[
                        0
                    ]
                    line = prefix + fragment
                    size = tokens(line) + 8
                    excerpts.append(row["id"])
                if used + size > cap or cost + size > budget:
                    continue
                selected.append(row["id"])
                blocks.append(line)
                used += size
                cost += size
        history, details = await self.continuity.build(person, message, summarize, history_budget)
        details.update(
            memory_candidates=[
                {"id": r["id"], "score": round(r["score"], 4), "selected": r["id"] in selected}
                for r in candidates
            ],
            memory_budget=budget,
            history_budget=history_budget or policy["history_max"],
            random_sampled=random_mem["id"] if random_mem else None,
            memory_excerpts=excerpts,
        )
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO context_runs(person_id,message_id,injected_ids,random_memory_id,estimated_memory_tokens,estimated_history_tokens,details)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    person,
                    message["id"],
                    Jsonb(selected),
                    random_mem["id"] if random_mem else None,
                    cost,
                    details["history_raw_tokens"],
                    Jsonb(details),
                ),
            )
        return "\n".join(blocks), history

    async def search(self, person, query, limit=16, budget=12000):
        await self.archive()
        rows = await self.candidates(person, query)
        result = []
        used = 0
        for row in rows[:limit]:
            item = {k: row[k] for k in ("id", "content", "status", "kind", "score")}
            cost = tokens(json.dumps(item, ensure_ascii=False)) + 2
            if used + cost <= budget:
                result.append(item)
                used += cost
        return result

    async def search_history(self, person, query, budget=12000):
        keywords = sorted(terms(query), key=lambda term: (-len(term), term))[:32]
        if isinstance(query, str) and len(query.strip()) >= 2:
            keywords = list(dict.fromkeys([query.strip(), *keywords]))[:33]
        if not keywords:
            return []
        async with self.pool.connection() as conn:
            # Rank before the result limit: recent generic matches must not
            # hide an older exact phrase or a stronger multi-keyword match.
            def like_literal(value):
                return (
                    "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )

            patterns = [like_literal(k) for k in keywords]
            rows = await (
                await conn.execute(
                    """SELECT id,role,content,created_at,conversation_id,
                    (CASE WHEN content ILIKE %s THEN 100 ELSE 0 END +
                    (SELECT count(*) FROM unnest(%s::text[]) AS needle WHERE content ILIKE needle)) AS match_score
                    FROM messages WHERE person_id=%s AND EXISTS(
                    SELECT 1 FROM unnest(%s::text[]) AS needle WHERE content ILIKE needle)
                    ORDER BY match_score DESC,id DESC LIMIT 120""",
                    (like_literal(query.strip()), patterns, person, patterns),
                )
            ).fetchall()
        out = []
        used = 0
        for row in rows:
            item = {**row, "created_at": str(row["created_at"])}
            if tokens(item["content"]) > min(3000, max(1, budget - used - 200)):
                content = item["content"]
                positions = [
                    content.lower().find(k.lower())
                    for k in keywords
                    if k.lower() in content.lower()
                ]
                offset = max(0, min(positions, default=0) - 1000)
                fragment = content[offset : offset + 5000]
                fragment = split_text(fragment, max(100, min(3000, budget - used - 200)))[0]
                item.update(
                    content=fragment,
                    excerpt=True,
                    source_start=offset,
                    source_end=offset + len(fragment),
                )
            size = tokens(json.dumps(item, ensure_ascii=False)) + 2
            if used + size <= budget:
                out.append(item)
                used += size
            if len(out) >= 12:
                break
        return out

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
        async with self.pool.connection() as conn:
            end = await (
                await conn.execute(
                    "SELECT max(id) AS n FROM messages WHERE person_id=%s AND (created_at AT TIME ZONE 'Asia/Shanghai')::date=%s",
                    (person, day),
                )
            ).fetchone()
        if not end["n"]:
            return {"status": "up_to_date"}
        changed = []
        batches = 0
        while True:
            result = await self._consolidate_batch(person, day, extract, end["n"])
            if result["status"] != "complete":
                if result["status"] == "up_to_date" and batches:
                    return {"status": "complete", "changed_ids": changed, "batches": batches}
                return result
            changed.extend(result["changed_ids"])
            batches += result["batches"]

    async def _consolidate_batch(self, person, day, extract, end_id):
        policy = await self.policy.get()
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
                        AND (created_at AT TIME ZONE 'Asia/Shanghai')::date=%s AND id>%s AND id<=%s ORDER BY id""",
                            (person, day, job["checkpoint"], end_id),
                        )
                    ).fetchall()
                    if not rows:
                        return {"status": "up_to_date"}
                    await conn.execute(
                        "UPDATE daily_jobs SET status='running',attempts=attempts+1,updated_at=now() WHERE person_id=%s AND day=%s",
                        (person, day),
                    )
                # Commit one contiguous source prefix. Later batches see the
                # newly created memory IDs and can correct them with evidence.
                cap = policy["daily_input"] - policy["daily_known"] - 2048
                prefix = []
                used = 0
                for row in rows:
                    cost = tokens(
                        json.dumps(
                            {k: row[k] for k in ("id", "role", "content")}, ensure_ascii=False
                        )
                    )
                    if prefix and used + cost > cap:
                        break
                    prefix.append(row)
                    used += cost
                rows = prefix
                batches, batch, size = [], [], 0
                for msg in rows:
                    for piece in split_text(msg["content"], cap - 512):
                        fragment = {**msg, "content": piece}
                        cost = tokens(
                            json.dumps(
                                {k: fragment[k] for k in ("id", "role", "content")},
                                ensure_ascii=False,
                            )
                        )
                        if size + cost > cap and batch:
                            batches.append(batch)
                            batch = []
                            size = 0
                        batch.append(fragment)
                        size += cost
                if batch:
                    batches.append(batch)
                existing = await self.memories(person)
                # Only bounded relevant existing memories are sent for duplicate/correction matching.
                day_terms = terms(" ".join(m["content"] for m in rows if m["role"] == "user"))
                existing.sort(key=lambda m: len(day_terms & terms(m["content"])), reverse=True)
                known, used = [], 0
                for m in existing:
                    item = {k: m[k] for k in ("id", "content", "kind")}
                    n = tokens(json.dumps(item, ensure_ascii=False))
                    if used + n <= policy["daily_known"]:
                        known.append(item)
                        used += n
                edits = []
                for batch in batches:
                    result = await extract(batch, known)
                    if not isinstance(result, list) or len(result) > 120:
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
                        or not isinstance(edit.get("content"), str)
                        or not edit["content"].strip()
                        or not 1 <= tokens(edit.get("content", "")) <= 2000
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
                prepared = {}
                if self.embedder:
                    contents = list(
                        dict.fromkeys(
                            e["content"] for e in edits if e["action"] in ("create", "replace")
                        )
                    )
                    if contents:
                        prepared = await self.prepare_chunks(contents)
                        vectors = {text: parts[0][1] for text, parts in prepared.items()}
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
                        if action in ("create", "replace"):
                            importance = edit.get(
                                "importance", 0.8 if edit["kind"] == "core" else 0.5
                            )
                            if not isinstance(importance, (int, float)) or not 0 <= importance <= 1:
                                raise ValueError("Memory importance must be between zero and one")
                            await conn.execute(
                                "UPDATE memories SET importance=%s WHERE id=%s", (importance, mid)
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
                            await self.write_chunks(conn, mid, prepared[edit["content"]])
                        await conn.execute(
                            "INSERT INTO memory_events(person_id,memory_id,action,message_id) VALUES (%s,%s,%s,%s)",
                            (person, mid, action, source["id"]),
                        )
                        changed.append(mid)
                    await conn.execute(
                        "UPDATE daily_jobs SET status='complete',checkpoint=%s,attempts=0,error=NULL,updated_at=now() WHERE person_id=%s AND day=%s",
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
