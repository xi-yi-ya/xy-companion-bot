"""Short conversation continuations on ContactStore's lock, ledger and timer."""

import math
import re
import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

DEFAULT_FOLLOWUP = {"enabled": True, "min_minutes": 8, "max_minutes": 20, "daily_limit": 2}
FOLLOWUP_PROMPT = """这是短期续聊候选检查，不是用户新消息，也不是定时问候任务。
根据最近完整对话，判断你是否真的还有值得补充的想法。默认保持安静。
只有话题尚未自然结束、补充具体且有价值、不是重复前文或催用户回答时，才 send=true。
用户说晚安、去忙、暂别、谢谢就这样、稍后再聊时不要重新打开话题；你上一句在等他回答时不要追问。
不能凭空制造未完话题、共同经历、已完成的行动，不要求用户立即回应，不输出调度说明。
沿用 L0/L1 和现有记忆，随机记忆无关就忽略；无工具权限，不更新人设或记忆。
返回 JSON：{"send":false,"text":"","reason":"判断的具体依据"}。
决定发送时 text 为一两句自然续话，不超过 250 Token；reason 简述承接哪个真实话题。
"""


def validate_followup(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_FOLLOWUP):
        raise ValueError("主动续聊设置字段不完整")
    if type(value["enabled"]) is not bool:
        raise ValueError("主动续聊开关无效")
    for key in ("min_minutes", "max_minutes"):
        if (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or not 2 <= value[key] <= 120
        ):
            raise ValueError("续聊时间窗口需要为 2～120 分钟")
    if value["min_minutes"] > value["max_minutes"]:
        raise ValueError("最短窗口不能大于最长窗口")
    if type(value["daily_limit"]) is not int or not 1 <= value["daily_limit"] <= 6:
        raise ValueError("续聊上限需要为 1～6 次 / 滚动 24 小时")
    return dict(value)


def local_reason(user, assistant):
    # Conservative hard stops before paying for a judgment. The model checks nuance again.
    if re.search(
        r"晚安|睡了|睡觉|去忙|先忙|不聊了|别找我|不要找我|别打扰|不打扰|下次再聊|回头聊|再见|就这样|休息吧|先这样|bye|good\s*night",
        user + "\n" + assistant,
        re.I,
    ):
        return "conversation_closed"
    if re.search(r"[？?][\s~～。！!]*$", assistant):
        return "waiting_for_answer"
    if len(user.strip()) < 8 or len(assistant.strip()) < 20:
        return "no_substantial_topic"
    return None


class FollowupMixin:
    async def followup_config(self, conn):
        row = await (
            await conn.execute("SELECT payload FROM followup_settings WHERE id=true")
        ).fetchone()
        return {**DEFAULT_FOLLOWUP, **(row["payload"] if row else {})}

    async def save_followup(self, value):
        value = validate_followup(value)
        async with self.pool.connection() as conn:
            await self.lock(conn)
            await conn.execute(
                "INSERT INTO followup_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                (Jsonb(value),),
            )
            await conn.execute(
                "UPDATE proactive_state SET followup_due=NULL,followup_reason='settings_changed'"
            )
            # Never undo a sending reservation or awaiting_reply on toggle.
            await conn.execute(
                "UPDATE proactive_state s SET current_run=NULL FROM proactive_deliveries d WHERE s.current_run=d.id AND d.kind='followup' AND d.status='generating'"
            )
            await conn.execute(
                "UPDATE proactive_deliveries SET status='cancelled',reason='settings_changed',updated_at=now() WHERE kind='followup' AND status='generating'"
            )
        return value

    async def note_reply(self, source, reply, now):
        from .proactive import OWNER, next_allowed

        if (
            source["person_id"] != OWNER
            or source.get("platform") != "aiocqhttp"
            or source.get("chat_type") != "private"
        ):
            return
        async with self.pool.connection() as conn:
            await self.lock(conn)
            settings = await self.followup_config(conn)
            contact = await self.config(conn)
            reason = local_reason(source["content"], reply)
            if not settings["enabled"]:
                reason = "disabled"
            seconds = self.rng.randint(
                round(settings["min_minutes"] * 60), round(settings["max_minutes"] * 60)
            )
            due, expires = (
                now + timedelta(seconds=seconds),
                now + timedelta(minutes=settings["max_minutes"] + 2),
            )
            if next_allowed(due, contact) > due:
                reason = "quiet_hours"
            await conn.execute(
                "UPDATE proactive_state SET followup_due=%s,followup_expires=%s,followup_reason=%s,followup_source=%s WHERE person_id=%s AND last_user_id=%s AND NOT awaiting_reply AND current_run IS NULL AND followup_source IS DISTINCT FROM %s",
                (
                    None if reason else due,
                    expires,
                    reason or "recent_conversation_candidate",
                    source["id"],
                    OWNER,
                    source["id"],
                    source["id"],
                ),
            )

    async def followup_claim(self, conn, state, settings, now, target):
        from .proactive import OWNER, next_allowed

        due = state.get("followup_due")
        if not due or due > now:
            return None
        policy = await self.followup_config(conn)
        reason = None
        if not policy["enabled"]:
            reason = "disabled"
        elif now > state["followup_expires"]:
            reason = "window_expired"
        elif next_allowed(now, settings) > now:
            reason = "quiet_hours"
        counts = await (
            await conn.execute(
                "SELECT count(*) AS checks,count(*) FILTER(WHERE status IN ('sending','sent','unknown')) AS sends FROM proactive_deliveries WHERE kind='followup' AND created_at>%s",
                (now - timedelta(hours=24),),
            )
        ).fetchone()
        if (
            counts["sends"] >= policy["daily_limit"]
            or counts["checks"] >= policy["daily_limit"] * 2
        ):
            reason = reason or "frequency_limit"
        if not reason and self.rng.randint(0, 99) >= 50:
            reason = "random_skip"
        await conn.execute(
            "UPDATE proactive_state SET followup_due=NULL,followup_reason=%s WHERE person_id=%s",
            (reason or "evaluating_unfinished_topic", OWNER),
        )
        uid = uuid.uuid4()
        row = await (
            await conn.execute(
                "INSERT INTO proactive_deliveries(id,person_id,source_user_id,target,status,reason,lease_until,kind,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'followup',%s) RETURNING *",
                (
                    uid,
                    OWNER,
                    state["last_user_id"],
                    Jsonb(target),
                    "skipped" if reason else "generating",
                    reason,
                    now + timedelta(minutes=4),
                    now,
                ),
            )
        ).fetchone()
        if reason:
            return None
        await conn.execute(
            "UPDATE proactive_state SET current_run=%s WHERE person_id=%s", (uid, OWNER)
        )
        return row
