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


def _protected_spans(text):
    """Offsets of common Markdown/quoted units; only surrounding prose may split."""
    spans = []
    lines = list(re.finditer(r"[^\n]*\n|[^\n]+$", text))

    def kind(line):
        if re.match(r"[ \t]*>", line):
            return "quote"
        if re.match(r"[ \t]*(?:[-*+] |\d+[.)、]\s)", line):
            return "list"
        if re.match(r"[ \t]*\|", line):
            return "table"
        if re.match(r"(?: {4}|\t)\S", line):
            return "indent"
        return None

    i = 0
    while i < len(lines):
        start = i
        fence = re.match(r" {0,3}(`{3,}|~{3,})", lines[i][0])
        block = kind(lines[i][0])
        if fence:
            marker = fence[1]
            i += 1
            while i < len(lines):
                closing = re.fullmatch(
                    r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\r?\n?",
                    lines[i][0],
                )
                i += 1
                if closing:
                    break
        elif block:
            i += 1
            while i < len(lines):
                following = kind(lines[i][0])
                if following == block or (block == "list" and lines[i][0].startswith(("  ", "\t"))):
                    i += 1
                elif not lines[i][0].strip():
                    nxt = i + 1
                    while nxt < len(lines) and not lines[nxt][0].strip():
                        nxt += 1
                    if nxt < len(lines) and (
                        kind(lines[nxt][0]) == block
                        or (block == "list" and lines[nxt][0].startswith(("  ", "\t")))
                    ):
                        i = nxt
                    else:
                        break
                else:
                    break
        else:
            i += 1
            continue
        spans.append((lines[start].start(), lines[i - 1].end()))

    for pattern in (
        r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)",
        r"https?://[^\s<>，。！？；、“”‘’]+",
        r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\r\n]*\"",
        r"\*\*[^*]+\*\*|__[^_]+__",
        r"\d+(?:[.,]\d+)+",
    ):
        spans.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.S))
    # Link destinations may contain nested parentheses; don't use a non-greedy
    # regex that would expose half a URL as ordinary sentence punctuation.
    for match in re.finditer(r"!?\[[^\]\r\n]*\]\(", text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            if text[end] == "\\":
                end += 2
                continue
            depth += (text[end] == "(") - (text[end] == ")")
            end += 1
        spans.append((match.start(), min(end, len(text))))
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def bubbles(text, strategy="natural"):
    """Lossless slices at thought boundaries, with no fixed bubble count or size."""
    if strategy == "single" or len(text) < 56:
        return [text]
    protected = _protected_spans(text)
    boundaries = {m.end(): 4 for m in re.finditer(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*", text)}
    if strategy == "natural":
        for m in re.finditer(r"\r?\n", text):
            if not re.match(r"[ \t]*\r?\n", text[m.end() :]):
                boundaries.setdefault(m.end(), 3)
        for m in re.finditer(r"(?:[。！？!?，,；;：:]+|\.(?=\s|$))[”’」』）)\]\"]*[ \t]*", text):
            if not text[m.end() :].startswith(("\n", "\r\n")):
                boundaries.setdefault(m.end(), 2 if re.search(r"[。！？!?.]", m[0]) else 1)
        for start, end in protected:
            # Only multiline units get their own boundary, not every inline URL.
            if "\n" in text[start:end]:
                boundaries.setdefault(start, 4)
                boundaries.setdefault(end, 4)
    safe, span = {}, 0
    for end, strength in sorted(boundaries.items()):
        while span < len(protected) and protected[span][1] <= end:
            span += 1
        if span < len(protected) and protected[span][0] < end < protected[span][1]:
            continue
        safe[end] = strength

    # Look ahead within a paragraph instead of requiring a large minimum length
    # at each comma. Prefer a full sentence, but choose an earlier clause if a
    # sentence-end cut would strand a tiny tail. Structure boundaries stay hard.
    groups, start = [], 0
    stops = sorted({p for p, strength in safe.items() if strength == 4} | {len(text)})
    for stop in stops:
        if stop <= start:
            continue
        while strategy == "natural" and stop - start > 64:
            candidates = []
            target = min(64, (stop - start) * 0.55)
            for end, strength in safe.items():
                if not 24 <= end - start <= 112 or stop - end < 18:
                    continue
                if not text[start:end].strip() or not text[end:stop].strip():
                    continue
                following = text[end:stop].lstrip()
                if following.startswith("而是"):
                    continue
                transition = bool(
                    re.match(r"不过|但是|但|所以|另外|至于|然后|其实|换句话说", following)
                )
                score = (
                    {1: 0, 2: 24, 3: 32, 4: 40}[strength]
                    + transition * 8
                    - abs(end - start - target)
                )
                candidates.append((score, end))
            if not candidates:
                break
            end = max(candidates)[1]
            groups.append(text[start:end])
            start = end
        unit = text[start:stop]
        if groups and not unit.strip():
            groups[-1] += unit
        else:
            groups.append(unit)
        start = stop
    return groups or [text]


def display_bubbles(text, strategy="natural"):
    """Paragraph separators become bubble boundaries, not empty lines on QQ.

    Only outer blank lines are presentation padding. Keep indentation and all
    internal newlines (notably code, lists and quotes) exactly as generated.
    """
    parts = []
    for raw in bubbles(text, strategy):
        part = re.sub(r"^(?:[ \t]*\r?\n)+|(?:\r?\n[ \t]*)+$", "", raw)
        if part.strip():
            parts.append(part)
    return parts


def delay(text, settings, *, elapsed=0, first=True, input_size=0, rng=random):
    if not settings["enabled"]:
        return 0
    lo, hi = settings["min_seconds"], settings["max_seconds"]
    weight = min(1, (len(text) + (input_size * 0.2 if first else 0)) / 220)
    target = lo + (hi - lo) * (0.65 * weight + 0.35 * rng.random())
    return max(0, target - elapsed) if first else min(1.2, target * 0.35)


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
            parts = display_bubbles(text, settings["strategy"] if settings["enabled"] else "single")
            gaps = [delay(part, settings, first=False) for part in parts[1:]]
            # More bubbles must not add an unbounded artificial typing wait.
            scale = min(1, min(4.0, settings["max_seconds"]) / sum(gaps)) if sum(gaps) else 1
            gaps = [gap * scale for gap in gaps]
            for index, part in enumerate(parts):
                seconds = (
                    delay(
                        part,
                        settings,
                        first=index == 0,
                        elapsed=max(0, self.clock() - received_at),
                        input_size=input_size,
                    )
                    if index == 0
                    else gaps[index - 1]
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
