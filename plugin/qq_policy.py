"""Persistent QQ delivery choices, independent of persona and memory decay."""

import json
import math
import re
import time

from psycopg.types.json import Jsonb

from .persona_requests import draft_blocks

DEFAULT_QQ_CHAT = {
    "group_scan_enabled": True,
    "group_scan_interval": 10,
    "group_ids": [],
    "quote_mode": "adaptive",
    "private_quiet_seconds": 3.5,
}


def validate_qq_chat(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_QQ_CHAT):
        raise ValueError("QQ 聊天设置字段不完整")
    if type(value["group_scan_enabled"]) is not bool:
        raise ValueError("群聊扫描开关需要是布尔值")
    interval = value["group_scan_interval"]
    if type(interval) is not int or not 1 <= interval <= 600:
        raise ValueError("群聊扫描间隔需要是 1～600 秒的整数")
    groups = value["group_ids"]
    if (
        not isinstance(groups, list)
        or len(groups) > 100
        or any(not isinstance(g, str) or not re.fullmatch(r"[1-9][0-9]{4,12}", g) for g in groups)
    ):
        raise ValueError("参与群号需要是有效的 QQ 群号列表，最多 100 个")
    if value["quote_mode"] not in ("adaptive", "never", "always"):
        raise ValueError("引用方式需要是按需、不引用或总是引用")
    quiet = value["private_quiet_seconds"]
    if type(quiet) not in (int, float) or not math.isfinite(quiet) or not 0.3 <= quiet <= 10:
        raise ValueError("私聊连发等待需要是 0.3～10 秒")
    return {**value, "group_ids": list(dict.fromkeys(groups))}


class QQChatPolicy:
    def __init__(self, pool):
        self.pool = pool
        self.cached, self.expires = None, 0

    async def get(self):
        if self.cached is not None and time.monotonic() < self.expires:
            return dict(self.cached)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT payload FROM qq_chat_settings WHERE id=true")
            ).fetchone()
        self.cached = {**DEFAULT_QQ_CHAT, **(row["payload"] if row else {})}
        self.expires = time.monotonic() + 0.5
        return dict(self.cached)

    async def save(self, value):
        value = validate_qq_chat(value)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO qq_chat_settings(id,payload) VALUES(true,%s) "
                "ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()",
                (Jsonb(value),),
            )
        self.cached, self.expires = value, time.monotonic() + 0.5
        return dict(value)


def allowed_group(event, settings):
    groups = settings["group_ids"]
    return not groups or str(event.get_group_id()) in groups


def quote_target(event, settings):
    if settings["quote_mode"] == "never":
        return None
    if settings["quote_mode"] == "adaptive":
        return event.get_extra("companion_quote_target")
    return {"id": str(event.message_obj.message_id), "sender": str(event.get_sender_id())}


def choose_quote(event, source_id):
    """Only real inputs visible in this turn can be selected for a quote."""
    entries = event.get_extra("companion_batch") or []
    for entry in entries:
        if str(entry.row["id"]) == str(source_id):
            target = {
                "id": str(entry.event.message_obj.message_id),
                "sender": str(entry.event.get_sender_id()),
            }
            event.set_extra("companion_quote_target", target)
            return target
    row = event.get_extra("companion_message")
    if row and str(row["id"]) == str(source_id):
        target = {"id": str(event.message_obj.message_id), "sender": str(event.get_sender_id())}
        event.set_extra("companion_quote_target", target)
        return target
    raise ValueError("只能引用本轮真实收到的消息编号")


async def recover_quote_reply(text, event, settings, regenerate):
    """Recover only an optional, current-input quote, never arbitrary tools."""
    if "companion_reply_to" not in text or not ("<tool_call>" in text or "DSML" in text):
        return text
    try:
        blocks = draft_blocks(text)
        if len(blocks) != 1 or len(blocks[0]) > 8000:
            raise ValueError("Ambiguous quote")
        call = json.loads(blocks[0])
        if call.get("name") != "companion_reply_to":
            return text
        args = call["arguments"]
        if (
            settings["quote_mode"] != "adaptive"
            or not isinstance(args, dict)
            or set(args) != {"message_id"}
        ):
            raise ValueError("Quote not allowed")
        choose_quote(event, args["message_id"])
    except (ValueError, KeyError, TypeError):
        return "这次没能选到可引用的消息，我先直接回你。"
    if "DSML" in text:
        clean = re.sub(
            r"<[|｜]{1,2}DSML[|｜]{1,2}\s*calls>.*?</[|｜]{1,2}DSML[|｜]{1,2}\s*calls>",
            "",
            text,
            flags=re.S,
        ).strip()
    else:
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.S).strip()
    if clean:
        return clean
    try:
        clean = (await regenerate()).strip()
    except Exception:
        clean = ""
    if not clean or "<tool_call>" in clean or "DSML" in clean:
        clean = "我指的是这条。"
    return clean
