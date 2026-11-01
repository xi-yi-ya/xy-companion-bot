"""Persistent, one-message-until-reply outreach using the companion's real identity."""

import asyncio
import logging
import math
import random
import re
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .token_counting import tokens

OWNER = "webchat:owner"
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CONTACT = {
    "enabled": True,
    "min_hours": 10,
    "max_hours": 14,
    "quiet_enabled": True,
    "quiet_start": "00:00",
    "quiet_end": "08:00",
}
CONTACT_PROMPT = """这是系统安排的后台主动联系任务，不是用户的新发言。
请沿用上面的 L0 固定人设、已批准 L1 和真实交流历史，自然地给用户发一两句轻消息。
优先承接确实聊过的事、尚未完成的话题；也可轻问近况。不要为了开口编造共同经历或声称做过并未执行的事。
用户表达忙碌、休息、暂时别找他时，应根据当前时间和原话判断是否仍应保持安静。没有合适内容也可以不发。
语气轻松，不责怪用户没回复，不要求用户马上回应，不暴露调度器、记忆编号或内部设定。
记忆是参考，随机旧事无关就忽略；本轮没有工具，不得刷新记忆、修改人设或声称执行了工具。
只返回 JSON：{"send":true或false,"text":"准备发出的自然短消息，不超过500 Token；不发时为空", "reason":"简短的内部原因"}。
"""


def validate_contact(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONTACT):
        raise ValueError("主动联系设置字段不完整")
    for key in ("enabled", "quiet_enabled"):
        if type(value[key]) is not bool:
            raise ValueError("主动联系开关无效")
    for key in ("min_hours", "max_hours"):
        if (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or not 0.25 <= value[key] <= 168
        ):
            raise ValueError("联系间隔需要在 0.25～168 小时之间")
    if value["min_hours"] > value["max_hours"]:
        raise ValueError("最短间隔不能大于最长间隔")
    for key in ("quiet_start", "quiet_end"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]
        ):
            raise ValueError("免打扰时间需要使用 HH:MM 格式")
    if value["quiet_enabled"] and value["quiet_start"] == value["quiet_end"]:
        raise ValueError("免打扰起止时间不能相同；全天可联系请关闭免打扰")
    return dict(value)


def next_allowed(at, settings):
    if not settings["quiet_enabled"]:
        return at
    local = at.astimezone(TZ)
    start, end = (time.fromisoformat(settings[k]) for k in ("quiet_start", "quiet_end"))
    current = local.time().replace(tzinfo=None)
    quiet = start <= current < end if start < end else current >= start or current < end
    if not quiet:
        return at
    day = local.date() + timedelta(days=int(start > end and current >= start))
    return datetime.combine(day, end, TZ).astimezone(UTC)


class ContactStore:
    def __init__(self, pool, rng=None):
        self.pool, self.rng = pool, rng or random.SystemRandom()

    async def lock(self, conn):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('companion:proactive:owner',0))"
        )

    async def config(self, conn):
        row = await (
            await conn.execute("SELECT payload FROM proactive_settings WHERE id=true")
        ).fetchone()
        return {**DEFAULT_CONTACT, **(row["payload"] if row else {})}

    def draw(self, settings):
        return self.rng.randint(
            round(settings["min_hours"] * 3600), round(settings["max_hours"] * 3600)
        )

    async def note_user(self, conn, message):
        if message["person_id"] != OWNER or message["role"] != "user":
            return
        await self.lock(conn)
        previous = await (
            await conn.execute(
                "SELECT last_user_id FROM proactive_state WHERE person_id=%s", (OWNER,)
            )
        ).fetchone()
        if previous and previous["last_user_id"] >= message["id"]:
            return
        settings = await self.config(conn)
        seconds = self.draw(settings)
        due = next_allowed(message["created_at"] + timedelta(seconds=seconds), settings)
        await conn.execute(
            """INSERT INTO proactive_state(person_id,last_user_id,last_user_at,due_at,interval_seconds)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(person_id) DO UPDATE SET
            last_user_id=EXCLUDED.last_user_id,last_user_at=EXCLUDED.last_user_at,due_at=EXCLUDED.due_at,
            interval_seconds=EXCLUDED.interval_seconds,awaiting_reply=false,current_run=NULL,attempts=0,updated_at=now()""",
            (OWNER, message["id"], message["created_at"], due, seconds),
        )
        await conn.execute(
            "UPDATE proactive_deliveries SET status='cancelled',reason='user_returned',updated_at=now() WHERE person_id=%s AND status='generating'",
            (OWNER,),
        )

    async def seed(self, now):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            latest = await (
                await conn.execute(
                    "SELECT * FROM messages WHERE person_id=%s AND role='user' ORDER BY id DESC LIMIT 1",
                    (OWNER,),
                )
            ).fetchone()
            # A removed technical test session must not leave an orphan timer.
            await conn.execute(
                "DELETE FROM proactive_state s WHERE person_id=%s AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.id=s.last_user_id)",
                (OWNER,),
            )
            if latest:
                await self.note_user(conn, latest)
            await conn.execute(
                "UPDATE proactive_state SET due_at=%s WHERE person_id=%s AND awaiting_reply=false AND current_run IS NULL AND due_at<%s",
                (now + timedelta(minutes=2), OWNER, now),
            )

    async def save(self, value, now):
        settings = validate_contact(value)
        async with self.pool.connection() as conn:
            await self.lock(conn)
            await conn.execute(
                "INSERT INTO proactive_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()",
                (Jsonb(settings),),
            )
            state = await (
                await conn.execute("SELECT * FROM proactive_state WHERE person_id=%s", (OWNER,))
            ).fetchone()
            if state and not state["awaiting_reply"]:
                seconds = self.draw(settings)
                due = next_allowed(
                    max(
                        state["last_user_at"] + timedelta(seconds=seconds),
                        now + timedelta(minutes=2),
                    ),
                    settings,
                )
                await conn.execute(
                    "UPDATE proactive_state SET due_at=%s,interval_seconds=%s,current_run=NULL,attempts=0,updated_at=now() WHERE person_id=%s",
                    (due, seconds, OWNER),
                )
                await conn.execute(
                    "UPDATE proactive_deliveries SET status='cancelled',reason='settings_changed',updated_at=now() WHERE person_id=%s AND status='generating'",
                    (OWNER,),
                )
        return settings

    async def snapshot(self):
        async with self.pool.connection() as conn:
            settings = await self.config(conn)
            state = await (
                await conn.execute("SELECT * FROM proactive_state WHERE person_id=%s", (OWNER,))
            ).fetchone()
            deliveries = await (
                await conn.execute(
                    "SELECT id,source_user_id,status,text,reason,error,created_at,sent_at,updated_at FROM proactive_deliveries WHERE person_id=%s ORDER BY created_at DESC LIMIT 20",
                    (OWNER,),
                )
            ).fetchall()
        return {
            "settings": settings,
            "state": state,
            "deliveries": deliveries,
            "timezone": "Asia/Shanghai",
        }

    async def recover(self, now, startup=False):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            rows = await (
                await conn.execute(
                    "SELECT * FROM proactive_deliveries WHERE person_id=%s AND status IN ('generating','sending') AND (%s OR lease_until<=%s)",
                    (OWNER, startup, now),
                )
            ).fetchall()
            for row in rows:
                uncertain = row["status"] == "sending"
                if uncertain:
                    await conn.execute(
                        "UPDATE qzone_candidates SET status='unknown' WHERE delivery_id=%s AND status='sending'",
                        (row["id"],),
                    )
                await conn.execute(
                    "UPDATE proactive_deliveries SET status=%s,error='interrupted',updated_at=now() WHERE id=%s",
                    ("unknown" if uncertain else "failed", row["id"]),
                )
                await conn.execute(
                    "UPDATE proactive_state SET current_run=NULL,due_at=%s,awaiting_reply=%s,updated_at=now() WHERE person_id=%s AND current_run=%s",
                    (
                        None if uncertain else now + timedelta(minutes=5),
                        uncertain,
                        OWNER,
                        row["id"],
                    ),
                )

    async def latest(self):
        async with self.pool.connection() as conn:
            source = await (
                await conn.execute(
                    "SELECT * FROM messages WHERE person_id=%s AND role='user' ORDER BY id DESC LIMIT 1",
                    (OWNER,),
                )
            ).fetchone()
            maximum = await (
                await conn.execute("SELECT max(id) AS n FROM messages WHERE person_id=%s", (OWNER,))
            ).fetchone()
        return source, (maximum["n"] or 0) + 1

    async def claim(self, now, target):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            settings = await self.config(conn)
            state = await (
                await conn.execute("SELECT * FROM proactive_state WHERE person_id=%s", (OWNER,))
            ).fetchone()
            if (
                not settings["enabled"]
                or not state
                or state["awaiting_reply"]
                or state["current_run"]
                or not state["due_at"]
                or state["due_at"] > now
                or state["attempts"] >= 3
            ):
                return None
            allowed = next_allowed(now, settings)
            if allowed > now:
                await conn.execute(
                    "UPDATE proactive_state SET due_at=%s WHERE person_id=%s", (allowed, OWNER)
                )
                return None
            source = await (
                await conn.execute(
                    "SELECT * FROM messages WHERE person_id=%s AND role='user' ORDER BY id DESC LIMIT 1",
                    (OWNER,),
                )
            ).fetchone()
            if not source or source["id"] != state["last_user_id"]:
                if source:
                    await self.note_user(conn, source)
                return None
            uid = uuid.uuid4()
            row = await (
                await conn.execute(
                    "INSERT INTO proactive_deliveries(id,person_id,source_user_id,target,status,lease_until) VALUES(%s,%s,%s,%s,'generating',%s) RETURNING *",
                    (uid, OWNER, source["id"], Jsonb(target), now + timedelta(minutes=10)),
                )
            ).fetchone()
            await conn.execute(
                "UPDATE proactive_state SET current_run=%s,attempts=attempts+1 WHERE person_id=%s",
                (uid, OWNER),
            )
            return row

    async def abort(self, run, reason, now, failed=False):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            settings = await self.config(conn)
            seconds = 300 if failed else self.draw(settings)
            due = next_allowed(now + timedelta(seconds=seconds), settings)
            await conn.execute(
                "UPDATE proactive_deliveries SET status=%s,reason=%s,error=%s,updated_at=now() WHERE id=%s AND status='generating'",
                (
                    "failed" if failed else "skipped",
                    reason[:500],
                    reason[:120] if failed else None,
                    run["id"],
                ),
            )
            await conn.execute(
                "UPDATE proactive_state SET current_run=NULL,due_at=CASE WHEN %s AND attempts>=3 THEN NULL ELSE %s END,attempts=CASE WHEN %s THEN attempts ELSE 0 END WHERE person_id=%s AND current_run=%s",
                (failed, due, failed, OWNER, run["id"]),
            )

    async def begin_send(self, run, result, now):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            settings = await self.config(conn)
            state = await (
                await conn.execute("SELECT * FROM proactive_state WHERE person_id=%s", (OWNER,))
            ).fetchone()
            latest = await (
                await conn.execute(
                    "SELECT max(id) AS n FROM messages WHERE person_id=%s AND role='user'", (OWNER,)
                )
            ).fetchone()
            candidate_id = result.get("qzone_candidate_id")
            qzone_ok = True
            if candidate_id is not None:
                config = await (
                    await conn.execute(
                        "SELECT payload,revision FROM qzone_settings WHERE id=true FOR SHARE"
                    )
                ).fetchone()
                candidate = await (
                    await conn.execute(
                        "SELECT id FROM qzone_candidates WHERE id=%s AND status='pending' AND account_qq=%s FOR UPDATE",
                        (candidate_id, str(run["target"].get("self_id", ""))),
                    )
                ).fetchone()
                qzone_ok = bool(
                    config
                    and config["payload"].get("enabled")
                    and config["payload"].get("share_enabled")
                    and config["revision"] == result.get("qzone_revision")
                    and candidate
                )
            if (
                not settings["enabled"]
                or not qzone_ok
                or not state
                or state["current_run"] != run["id"]
                or state["last_user_id"] != run["source_user_id"]
                or latest["n"] != run["source_user_id"]
                or next_allowed(now, settings) > now
            ):
                await conn.execute(
                    "UPDATE proactive_deliveries SET status='cancelled',reason='pre_send_check',updated_at=now() WHERE id=%s AND status='generating'",
                    (run["id"],),
                )
                if state and state["current_run"] == run["id"]:
                    await conn.execute(
                        "UPDATE proactive_state SET current_run=NULL,due_at=%s WHERE person_id=%s",
                        (next_allowed(now + timedelta(minutes=5), settings), OWNER),
                    )
                return False
            # Consume the cycle BEFORE network I/O. Uncertain delivery never retries.
            if candidate_id is not None:
                await conn.execute(
                    "UPDATE qzone_candidates SET status='sending',delivery_id=%s WHERE id=%s",
                    (run["id"], candidate_id),
                )
            await conn.execute(
                "UPDATE proactive_deliveries SET status='sending',text=%s,reason=%s,updated_at=now() WHERE id=%s",
                (result["text"], result["reason"][:500], run["id"]),
            )
            await conn.execute(
                "UPDATE proactive_state SET awaiting_reply=true,due_at=NULL WHERE person_id=%s",
                (OWNER,),
            )
            return True

    async def finish(self, run, text, now, receipt=None, error=None):
        async with self.pool.connection() as conn:
            await self.lock(conn)
            await conn.execute(
                "UPDATE qzone_candidates SET status=%s WHERE delivery_id=%s AND status='sending'",
                ("unknown" if error else "shared", run["id"]),
            )
            if error is None:
                await conn.execute(
                    "INSERT INTO messages(person_id,conversation_id,event_id,role,content,created_at,token_count,platform,platform_id,chat_type,sender_id) VALUES(%s,%s,%s,'assistant',%s,%s,%s,'aiocqhttp',%s,'private',%s) ON CONFLICT DO NOTHING",
                    (
                        OWNER,
                        run["target"]["umo"],
                        "proactive:" + str(run["id"]),
                        text,
                        now,
                        tokens(text),
                        run["target"]["umo"].split(":", 1)[0],
                        str(run["target"].get("self_id") or "") or None,
                    ),
                )
            await conn.execute(
                "UPDATE proactive_deliveries SET status=%s,receipt=%s,error=%s,sent_at=%s,updated_at=now() WHERE id=%s",
                (
                    "unknown" if error else "sent",
                    Jsonb(receipt or {}),
                    error,
                    None if error else now,
                    run["id"],
                ),
            )
            await conn.execute(
                "UPDATE proactive_state SET current_run=NULL,updated_at=now() WHERE person_id=%s AND current_run=%s",
                (OWNER, run["id"]),
            )


class ContactService:
    def __init__(self, store, generate, send, available, busy, current, clock=None):
        self.store, self.generate, self.send = store, generate, send
        self.available, self.busy, self.current = available, busy, current
        self.clock = clock or (lambda: datetime.now(UTC))
        self.task = None
        self.last_error = None

    @staticmethod
    def validate(result):
        if (
            not isinstance(result, dict)
            or type(result.get("send")) is not bool
            or not isinstance(result.get("text"), str)
            or not isinstance(result.get("reason"), str)
        ):
            raise ValueError("主动消息格式无效")
        if result["send"] and (not result["text"].strip() or tokens(result["text"]) > 500):
            raise ValueError("主动消息为空或超过短消息预算")
        return {**result, "text": result["text"].strip()}

    async def step(self):
        await self.store.recover(self.clock())
        if self.busy():
            return
        target = await self.available()
        if not target:
            return
        run = await self.store.claim(self.clock(), target)
        if not run:
            return
        try:
            async with asyncio.timeout(180):
                result = self.validate(await self.generate(run))
            if not result["send"]:
                await self.store.abort(run, result["reason"], self.clock())
                return
            if await self.available() != target or not await self.current(result):
                await self.store.abort(run, "target_or_persona_changed", self.clock())
                return
        except asyncio.CancelledError:
            await self.store.abort(run, "generation_cancelled", self.clock(), True)
            raise
        except Exception as exc:
            await self.store.abort(run, type(exc).__name__, self.clock(), True)
            return
        if not await self.store.begin_send(run, result, self.clock()):
            return
        try:
            async with asyncio.timeout(30):
                receipt = await self.send(target, result["text"])
            if not isinstance(receipt, dict) or not receipt.get("message_id"):
                raise ValueError("QQ 未返回消息回执")
        except BaseException as exc:
            await self.store.finish(run, result["text"], self.clock(), error=type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
        else:
            await self.store.finish(run, result["text"], self.clock(), receipt=receipt)

    async def run(self):
        initialized = False
        while True:
            try:
                if not initialized:
                    await self.store.recover(self.clock(), startup=True)
                    await self.store.seed(self.clock())
                    initialized = True
                await self.step()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A database outage cannot turn into an unrecorded outbound send.
                self.last_error = type(exc).__name__
                logging.getLogger(__name__).warning("Proactive scheduler: %s", self.last_error)
            await asyncio.sleep(60)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
