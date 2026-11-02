"""Bounded pacing inside the existing QQ response task; no outbound worker queue."""

import asyncio
import math
import random
import re
import time
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb

DEFAULT_RHYTHM = {
    "enabled": True,
    "min_seconds": 1.5,
    "max_seconds": 5.0,
    "strategy": "natural",
}


def validate_rhythm(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_RHYTHM):
        raise ValueError("消息节奏设置字段不完整")
    if type(value["enabled"]) is not bool or value["strategy"] not in (
        "natural",
        "paragraph",
        "single",
    ):
        raise ValueError("消息节奏开关或分段策略无效")
    for key in ("min_seconds", "max_seconds"):
        if (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or not 0 <= value[key] <= 10
        ):
            raise ValueError("延迟范围需要为 0～10 秒")
    if value["min_seconds"] > value["max_seconds"]:
        raise ValueError("最短延迟不能大于最长延迟")
    return dict(value)


def bubbles(text, strategy="natural"):
    """Conservative boundaries. A structured reply stays intact, including whitespace."""
    if strategy == "single" or re.search(
        r"```|~~~|`|https?://|\[[^\]]*\]\(|(?m:^\s*(?:>|#|[-*+]\s|\d+[.)、]\s|\|))|[{}<>]|[“”「」『』\"]",
        text,
    ):
        return [text]
    parts = re.split(r"\n\s*\n", text)
    if len(parts) == 1 and strategy == "natural" and 50 <= len(text) <= 450:
        parts = re.split(r"(?<=[。！？!?])(?=\S)", text)
    if len(parts) <= 1:
        return [text]
    # Keep natural units; never cut a long sentence to hit a character budget.
    groups = []
    for part in parts:
        if not part.strip():
            continue
        if groups and len(groups[-1]) < 24:
            groups[-1] += "\n\n" + part
        else:
            groups.append(part)
    if len(groups) > 3:
        groups = groups[:2] + ["\n\n".join(groups[2:])]
    return groups or [text]


def delay(text, settings, *, elapsed=0, first=True, input_size=0, rng=random):
    if not settings["enabled"]:
        return 0
    lo, hi = settings["min_seconds"], settings["max_seconds"]
    weight = min(1, (len(text) + (input_size * 0.2 if first else 0)) / 220)
    target = lo + (hi - lo) * (0.65 * weight + 0.35 * rng.random())
    return max(0, target - elapsed) if first else min(2.5, target)


class Rhythm:
    def __init__(self, pool, clock=time.monotonic, sleep=asyncio.sleep):
        self.pool, self.clock, self.sleep = pool, clock, sleep
        self.locks = {}
        self.tasks = set()
        self.closing = False

    async def settings(self):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT payload FROM rhythm_settings WHERE id=true")
            ).fetchone()
        return {**DEFAULT_RHYTHM, **(row["payload"] if row else {})}

    async def save(self, value):
        value = validate_rhythm(value)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO rhythm_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                (Jsonb(value),),
            )
        return value

    @asynccontextmanager
    async def lane(self, key):
        # Reference-count waiters as well as the holder; do not leak chat locks.
        entry = self.locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with entry[0]:
                yield
        finally:
            self.tasks.discard(task)
            entry[1] -= 1
            if not entry[1]:
                self.locks.pop(key, None)

    async def deliver(
        self, key, text, received_at, current, send, record, *, input_size=0, source_id=None
    ):
        if source_id is not None:
            async with self.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "INSERT INTO qq_reply_runs(source_id,status) VALUES(%s,'running') ON CONFLICT DO NOTHING RETURNING source_id",
                        (source_id,),
                    )
                ).fetchone()
            if not row:
                return {"status": "duplicate", "reason": "already_claimed", "sent": []}
        outcome = {"status": "unknown", "reason": "interrupted", "sent": []}
        try:
            outcome = await self._deliver(
                key,
                text,
                received_at,
                current,
                send,
                record,
                input_size=input_size,
                sent=outcome["sent"],
            )
            return outcome
        except Exception as exc:
            outcome["reason"] = type(exc).__name__
            return outcome
        finally:
            if source_id is not None:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE qq_reply_runs SET status=%s,reason=%s,sent_count=%s,updated_at=now() WHERE source_id=%s",
                        (outcome["status"], outcome["reason"], len(outcome["sent"]), source_id),
                    )

    async def recover(self):
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE qq_reply_runs SET status='unknown',reason='restart_no_replay',updated_at=now() WHERE status='running'"
            )

    async def recent(self):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute("SELECT * FROM qq_reply_runs ORDER BY created_at DESC LIMIT 10")
            ).fetchall()

    async def _deliver(self, key, text, received_at, current, send, record, *, input_size=0, sent):
        """send uses the existing durable QQ operation idempotency key per bubble."""
        async with self.lane(key):
            settings = await self.settings()
            parts = bubbles(text, settings["strategy"] if settings["enabled"] else "single")
            for index, part in enumerate(parts):
                seconds = delay(
                    part,
                    settings,
                    first=index == 0,
                    elapsed=max(0, self.clock() - received_at),
                    input_size=input_size,
                )
                until = self.clock() + seconds
                while self.clock() < until:
                    if self.closing or not await current():
                        return {
                            "status": "cancelled",
                            "reason": "new_input_or_permission",
                            "sent": sent,
                        }
                    await self.sleep(min(0.1, max(0, until - self.clock())))
                if self.closing or not await current():
                    return {
                        "status": "cancelled",
                        "reason": "new_input_or_permission",
                        "sent": sent,
                    }
                # Changing settings during a wait cancels the unsent tail, never re-splits it.
                if await self.settings() != settings:
                    return {"status": "cancelled", "reason": "settings_changed", "sent": sent}
                async with asyncio.timeout(30):
                    receipt = await send(part, index)
                if receipt.get("status") != "sent":
                    return {
                        "status": receipt.get("status", "unknown"),
                        "reason": "delivery_stopped",
                        "sent": sent,
                    }
                sent.append(part)
                await record(part, index)
            return {"status": "sent", "reason": "complete", "sent": sent}

    async def close(self):
        self.closing = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
