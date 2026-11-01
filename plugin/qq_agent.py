"""Scoped OneBot actions with persistent request inbox, receipts and a kill switch."""

import asyncio
import base64
import hashlib
import json
import re
import uuid
from contextvars import ContextVar
from pathlib import Path

from psycopg.types.json import Jsonb

DEFAULT_AGENT = {
    "paused": False,
    "friend_policy": "manual",
    "friend_allowlist": [],
    "invite_policy": "manual",
    "group_allowlist": [],
    "private_ids": [],
    "group_rules": [],
    "send_images": True,
    "send_files": True,
    "recall": True,
}
ID = re.compile(r"[1-9][0-9]{4,12}")
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
FILES = MEDIA | {
    ".doc",
    ".docx",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".xlsx",
    ".pptx",
    ".json",
    ".zip",
    ".mp3",
    ".mp4",
}
MANAGED_ACTION = ContextVar("companion_qq_managed_action", default=False)


def validate_agent(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_AGENT):
        raise ValueError("QQ 权限设置字段不完整")
    for key in ("paused", "send_images", "send_files", "recall"):
        if type(value[key]) is not bool:
            raise ValueError("QQ 权限开关必须是布尔值")
    for key in ("friend_policy", "invite_policy"):
        if value[key] not in ("manual", "allowlist"):
            raise ValueError("申请策略必须是 manual 或 allowlist")
    for key in ("friend_allowlist", "group_allowlist", "private_ids"):
        if (
            not isinstance(value[key], list)
            or len(value[key]) > 100
            or any(not isinstance(x, str) or not ID.fullmatch(x) for x in value[key])
        ):
            raise ValueError("QQ / 群号白名单无效")
    rules = value["group_rules"]
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("群权限列表无效")
    seen = set()
    for rule in rules:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"group_id", "read", "reply", "media"}
            or not isinstance(rule["group_id"], str)
            or not ID.fullmatch(rule["group_id"])
            or rule["group_id"] in seen
            or any(type(rule[k]) is not bool for k in ("read", "reply", "media"))
        ):
            raise ValueError("每个群需独立设置群号、读取、回复和发附件权限，群号不能重复")
        if rule["reply"] and not rule["read"] or rule["media"] and not rule["reply"]:
            raise ValueError("群附件权限需要回复权限，回复权限需要读取权限")
        seen.add(rule["group_id"])
    return json.loads(json.dumps(value))


def group_permission(settings, group_id, action):
    rule = next((r for r in settings["group_rules"] if r["group_id"] == str(group_id)), None)
    return bool(rule[action]) if rule else False


def routed_arguments(account, args):
    values = dict(args)
    supplied = values.pop("self_id", account)
    if str(supplied) != str(account):
        raise ValueError("只能操作当前 Bot 小号")
    return {**values, "self_id": str(account)}


class QQAgent:
    def __init__(self, pool, call, identity, root):
        self.pool, self.call, self.identity = pool, call, identity
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = set()
        self.closing = False
        self.transports = []
        self.known_accounts = set()
        self.inventory_lock = asyncio.Lock()

    async def ensure_inventory(self):
        account = str(self.identity().get("bot_qq") or "")
        if (
            not account
            or not self.identity().get("online")
            or account in self.known_accounts
            or (await self.settings())["paused"]
        ):
            return
        async with self.inventory_lock:
            if account in self.known_accounts:
                return
            async with self.pool.connection() as conn:
                exists = await (
                    await conn.execute(
                        "SELECT 1 FROM qq_agent_accounts WHERE account_qq=%s", (account,)
                    )
                ).fetchone()
            if not exists:
                groups = await self.call("get_group_list")
                if not isinstance(groups, list):
                    raise ValueError("无法读取既有群权限")
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        ("companion:qq-inventory:" + account,),
                    )
                    current = await (
                        await conn.execute(
                            "SELECT payload FROM qq_agent_settings WHERE id=true FOR UPDATE"
                        )
                    ).fetchone()
                    value = json.loads(
                        json.dumps({**DEFAULT_AGENT, **(current["payload"] if current else {})})
                    )
                    old = await (
                        await conn.execute("SELECT payload FROM qq_chat_settings WHERE id=true")
                    ).fetchone()
                    allowed = old["payload"].get("group_ids", []) if old else []
                    for group in groups:
                        gid = str(group.get("group_id", ""))
                        if ID.fullmatch(gid) and not any(
                            r["group_id"] == gid for r in value["group_rules"]
                        ):
                            enabled = not allowed or gid in allowed
                            value["group_rules"].append(
                                {"group_id": gid, "read": enabled, "reply": enabled, "media": False}
                            )
                    await conn.execute(
                        "INSERT INTO qq_agent_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                        (Jsonb(value),),
                    )
                    await conn.execute(
                        "INSERT INTO qq_agent_accounts(account_qq) VALUES(%s) ON CONFLICT DO NOTHING",
                        (account,),
                    )
            self.known_accounts.add(account)

    async def readonly_group(self, group_id):
        async with self.pool.connection() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('companion:qq-agent-settings',0))"
            )
            current = await (
                await conn.execute("SELECT payload FROM qq_agent_settings WHERE id=true FOR UPDATE")
            ).fetchone()
            value = json.loads(
                json.dumps({**DEFAULT_AGENT, **(current["payload"] if current else {})})
            )
            if not any(r["group_id"] == group_id for r in value["group_rules"]):
                value["group_rules"].append(
                    {"group_id": group_id, "read": True, "reply": False, "media": False}
                )
                await conn.execute(
                    "INSERT INTO qq_agent_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                    (Jsonb(value),),
                )

    async def notice(self, raw):
        if (
            isinstance(raw, dict)
            and raw.get("notice_type") in ("friend_recall", "group_recall")
            and str(raw.get("self_id")) == str(self.identity().get("bot_qq"))
            and str(raw.get("user_id")) == str(self.identity().get("bot_qq"))
        ):
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE qq_operations SET recalled_at=now(),metadata=metadata||%s WHERE account_qq=%s AND message_id=%s AND action IN ('send_private_msg','send_group_msg','send_msg')",
                    (
                        Jsonb({"recall_confirmed_by": "QQ_notice"}),
                        str(raw["self_id"]),
                        str(raw.get("message_id")),
                    ),
                )
        if (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("notice_type") == "group_increase"
            and str(raw.get("user_id")) == str(self.identity().get("bot_qq"))
            and ID.fullmatch(str(raw.get("group_id", "")))
        ):
            await self.readonly_group(str(raw["group_id"]))

    def install_transport(self, bot):
        if self.closing:
            return
        attrs = vars(bot)
        if attrs.get("_companion_qq_agent") is self:
            return
        original = attrs.get("_companion_qq_original_action")
        if original is None:
            original = bot.call_action
            attrs["_companion_qq_original_action"] = original

            async def action(action, **params):
                if (
                    action not in ("send_private_msg", "send_group_msg", "send_msg")
                    or MANAGED_ACTION.get()
                ):
                    return await original(action, **params)
                active = vars(bot).get("_companion_qq_agent")
                if active is None or active.closing:
                    raise ValueError("QQ 操作层未运行")
                identity = active.identity()
                if str(params.get("self_id") or identity.get("bot_qq", "")) != str(
                    identity.get("bot_qq", "")
                ):
                    raise ValueError("只能操作当前 Bot 小号")
                group = params.get("group_id")
                target_type, target_id = (
                    ("group", str(group)) if group else ("private", str(params.get("user_id", "")))
                )
                chain = params.get("message", [])
                kinds = (
                    {x.get("type") for x in chain if isinstance(x, dict)}
                    if isinstance(chain, list)
                    else set()
                )
                kind = "file" if "file" in kinds else "image" if "image" in kinds else "text"
                result = await active.execute(
                    action,
                    params,
                    "framework:" + str(uuid.uuid4()),
                    "framework",
                    {"target_type": target_type, "target_id": target_id, "asset_kind": kind},
                )
                if result["status"] != "sent":
                    raise ValueError("QQ 发送未确认：" + result["status"])
                return {"message_id": result["message_id"]}

            bot.call_action = action
        attrs["_companion_qq_agent"] = self
        self.transports.append(bot)

    def uninstall_transport(self):
        for bot in self.transports:
            attrs = vars(bot)
            if attrs.get("_companion_qq_agent") is self:
                bot.call_action = attrs.pop("_companion_qq_original_action")
                attrs.pop("_companion_qq_agent", None)
        self.transports.clear()

    async def settings(self):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT payload FROM qq_agent_settings WHERE id=true")
            ).fetchone()
        return json.loads(json.dumps({**DEFAULT_AGENT, **(row["payload"] if row else {})}))

    async def recover(self):
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE qq_operations SET status='unknown',error='服务重启前未取得回执，不会自动重试',finished_at=now() WHERE status='sending'"
            )

    async def save(self, value):
        value = validate_agent(value)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO qq_agent_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                (Jsonb(value),),
            )
        return value

    async def inbox(self, raw, platform_id):
        if not isinstance(raw, dict) or raw.get("post_type") != "request":
            return None
        kind, subtype = raw.get("request_type"), raw.get("sub_type", "")
        if kind not in ("friend", "group") or kind == "group" and subtype != "invite":
            return {"status": "unsupported", "reason": "不处理其他成员入群审批"}
        if (
            not raw.get("flag")
            or not ID.fullmatch(str(raw.get("self_id", "")))
            or not ID.fullmatch(str(raw.get("user_id", "")))
        ):
            return {"status": "invalid"}
        group = str(raw.get("group_id", "")) if kind == "group" else None
        if kind == "group" and not ID.fullmatch(group):
            return {"status": "invalid"}
        key = hashlib.sha256(f"{raw['self_id']}:{kind}:{raw['flag']}".encode()).hexdigest()
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO qq_requests(id,account_qq,platform_id,kind,sub_type,user_id,group_id,flag,comment) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET id=EXCLUDED.id RETURNING *",
                    (
                        key,
                        str(raw["self_id"]),
                        platform_id,
                        kind,
                        subtype,
                        str(raw["user_id"]),
                        group,
                        str(raw["flag"]),
                        str(raw.get("comment", ""))[:2000],
                    ),
                )
            ).fetchone()
        settings = await self.settings()
        allowed = self.request_allowed(row, settings)
        if allowed and not settings["paused"]:
            return await self.handle(key, True, actor="policy")
        return {"status": row["status"], "request_id": key}

    @staticmethod
    def request_allowed(row, settings):
        return (
            (
                settings["friend_policy"] == "allowlist"
                and row["user_id"] in settings["friend_allowlist"]
            )
            if row["kind"] == "friend"
            else (
                settings["invite_policy"] == "allowlist"
                and row["group_id"] in settings["group_allowlist"]
            )
        )

    async def handle(self, request_id, approve, *, actor="bot"):
        if type(approve) is not bool:
            raise ValueError("approve 必须是布尔值")
        settings = await self.settings()
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT * FROM qq_requests WHERE id=%s", (request_id,))
            ).fetchone()
        if not row:
            raise ValueError("申请不存在")
        if row["status"] != "pending":
            return {"status": row["status"], "request_id": request_id}
        identity = self.identity()
        if row["account_qq"] != str(identity.get("bot_qq", "")):
            raise ValueError("申请属于另一个 QQ 账号")
        if actor != "owner_ui" and not self.request_allowed(row, settings):
            return {
                "status": "approval_required",
                "reason": "不在自动处理白名单，请在 Bot 设置的待处理申请中决定",
            }
        action = "set_friend_add_request" if row["kind"] == "friend" else "set_group_add_request"
        args = {"flag": row["flag"], "approve": approve}
        if row["kind"] == "friend":
            args["remark"] = ""
        else:
            args.update(sub_type="invite", reason="")
            if approve:
                await self.readonly_group(row["group_id"])
        result = await self.execute(
            action,
            args,
            "request:" + request_id,
            actor,
            {
                "request_id": request_id,
                "approve": approve,
                "user_id": row["user_id"],
                "group_id": row["group_id"],
            },
        )
        if result["status"] == "sent":
            async with self.pool.connection() as conn:
                operation = await (
                    await conn.execute(
                        "SELECT metadata FROM qq_operations WHERE id=%s", (result["operation_id"],)
                    )
                ).fetchone()
                if operation["metadata"]["approve"] != approve:
                    return {"status": "conflict", "reason": "已有另一项处理决定，未重复操作"}
                await conn.execute(
                    "UPDATE qq_requests SET status=%s,decided_at=now() WHERE id=%s",
                    ("approved" if approve else "rejected", request_id),
                )
                if row["kind"] == "group" and approve:
                    # New rooms start read-only; replying requires a separate permission.
                    current = await (
                        await conn.execute(
                            "SELECT payload FROM qq_agent_settings WHERE id=true FOR UPDATE"
                        )
                    ).fetchone()
                    value = json.loads(
                        json.dumps({**DEFAULT_AGENT, **(current["payload"] if current else {})})
                    )
                    if not any(r["group_id"] == row["group_id"] for r in value["group_rules"]):
                        value["group_rules"].append(
                            {
                                "group_id": row["group_id"],
                                "read": True,
                                "reply": False,
                                "media": False,
                            }
                        )
                        await conn.execute(
                            "INSERT INTO qq_agent_settings(id,payload) VALUES(true,%s) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                            (Jsonb(value),),
                        )
        return result

    async def permitted(self, action, actor, metadata):
        settings = await self.settings()
        if self.closing or settings["paused"]:
            return False
        if action in ("send_private_msg", "send_group_msg", "send_msg"):
            kind = metadata["asset_kind"]
            if kind != "text" and not settings["send_images" if kind == "image" else "send_files"]:
                return False
            if metadata["target_type"] == "group":
                return group_permission(
                    settings, metadata["target_id"], "reply" if kind == "text" else "media"
                )
            return metadata["target_id"] in [
                str(self.identity().get("owner_qq", "")),
                *settings["private_ids"],
            ]
        if action in ("set_friend_add_request", "set_group_add_request") and actor != "owner_ui":
            return self.request_allowed(
                {"kind": "friend" if action == "set_friend_add_request" else "group", **metadata},
                settings,
            )
        return action != "delete_msg" or settings["recall"]

    async def execute(self, action, args, key, actor, metadata):
        if self.closing or (await self.settings())["paused"]:
            return {"status": "paused", "reason": "QQ 急停已开启"}
        identity = self.identity()
        if not identity.get("online"):
            return {"status": "offline"}
        account = str(identity["bot_qq"])
        operation = uuid.uuid4()
        async with self.pool.connection() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("companion:qq-actions:" + account,),
            )
            if action.startswith("send_") and metadata.get("asset_kind") != "text":
                rate = await (
                    await conn.execute(
                        "SELECT count(*) FILTER(WHERE created_at>now()-interval '1 minute') AS minute,count(*) FILTER(WHERE created_at>now()-interval '24 hours') AS day FROM qq_operations WHERE account_qq=%s AND action IN ('send_private_msg','send_group_msg','send_msg') AND metadata->>'asset_kind' IN ('image','file') AND status IN ('sending','sent','unknown')",
                        (account,),
                    )
                ).fetchone()
                if rate["minute"] >= 10 or rate["day"] >= 100:
                    return {
                        "status": "rate_limited",
                        "reason": "附件发送上限：每分钟 10 条、24 小时 100 条",
                    }
            row = await (
                await conn.execute(
                    "INSERT INTO qq_operations(id,account_qq,action,idempotency_key,actor,metadata,status) VALUES(%s,%s,%s,%s,%s,%s,'sending') ON CONFLICT(account_qq,idempotency_key) DO NOTHING RETURNING *",
                    (operation, account, action, key, actor, Jsonb(metadata)),
                )
            ).fetchone()
            if not row:
                old = await (
                    await conn.execute(
                        "SELECT id,status,message_id FROM qq_operations WHERE account_qq=%s AND idempotency_key=%s",
                        (account, key),
                    )
                ).fetchone()
                return {
                    "operation_id": str(old["id"]),
                    "status": "unknown" if old["status"] == "sending" else old["status"],
                    "message_id": old["message_id"],
                }
        task = asyncio.current_task()
        self.active.add(task)
        result, status, error = {}, "unknown", None
        try:
            # Recheck immediately before external I/O, after durable reservation.
            if (
                not await self.permitted(action, actor, metadata)
                or str(self.identity().get("bot_qq", "")) != account
            ):
                status = "cancelled"
            else:
                token = MANAGED_ACTION.set(True)
                try:
                    result = await self.call(action, **args)
                finally:
                    MANAGED_ACTION.reset(token)
                result = result or {}
                if not isinstance(result, dict):
                    raise ValueError("API receipt invalid")
                if result.get("status") == "failed" or result.get("retcode", 0) != 0:
                    status, error = "failed", "OneBot rejected"
                elif action.startswith("send_") and not result.get("message_id"):
                    error = "发送回执缺少消息编号，结果未知"
                else:
                    status = "sent"
        except asyncio.CancelledError:
            error = "任务中断，结果未知，不会自动重试"
            raise
        except Exception as exc:
            error = type(exc).__name__
            result_error = getattr(exc, "result", None)
            if isinstance(result_error, dict):
                detail = str(result_error.get("message") or result_error.get("wording") or "")
                detail = re.sub(
                    r"https?://\S+|(?i:sk-)[\w-]+|[a-zA-Z0-9_=-]{24,}", "[已隐藏]", detail
                )[:200]
                error += f"(retcode={result_error.get('retcode', 'unknown')}): " + detail
            status = (
                "failed" if type(exc).__name__ in ("ActionFailed", "ApiNotAvailable") else "unknown"
            )
            if re.search(r"timeout|timed out|超时", error, re.I):
                status = "unknown"
        finally:
            try:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE qq_operations SET status=%s,message_id=%s,error=%s,finished_at=now() WHERE id=%s",
                        (
                            status,
                            str(result.get("message_id")) if result.get("message_id") else None,
                            error,
                            operation,
                        ),
                    )
            finally:
                self.active.discard(task)
        return {
            "operation_id": str(operation),
            "status": status,
            "message_id": result.get("message_id"),
            "error": error,
        }

    async def directory(self, kind, group_id=""):
        if kind not in ("friends", "groups", "members"):
            raise ValueError("目录类型无效")
        if (await self.settings())["paused"]:
            return {"status": "paused"}
        if kind == "members" and not ID.fullmatch(str(group_id)):
            raise ValueError("群号无效")
        if kind == "members" and not group_permission(await self.settings(), group_id, "read"):
            raise ValueError("该群的读取权限已关闭")
        if kind == "members":
            groups = await self.call("get_group_list")
            if not any(str(g.get("group_id")) == group_id for g in groups):
                raise ValueError("Bot 不在这个群内")
        action = {
            "friends": "get_friend_list",
            "groups": "get_group_list",
            "members": "get_group_member_list",
        }[kind]
        operation = uuid.uuid4()
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO qq_operations(id,account_qq,action,idempotency_key,actor,metadata,status) VALUES(%s,%s,%s,%s,'bot',%s,'reading')",
                (
                    operation,
                    str(self.identity().get("bot_qq", "")),
                    action,
                    str(operation),
                    Jsonb({"group_id": group_id, "kind": kind}),
                ),
            )
        task = asyncio.current_task()
        self.active.add(task)
        try:
            rows = await self.call(
                action, **({"group_id": int(group_id)} if kind == "members" else {})
            )
            if not isinstance(rows, list):
                raise ValueError("QQ 名录返回格式无效")
        except BaseException as exc:
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE qq_operations SET status='failed',error=%s,finished_at=now() WHERE id=%s",
                    (type(exc).__name__, operation),
                )
            raise
        else:
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE qq_operations SET status='read',metadata=metadata||%s,finished_at=now() WHERE id=%s",
                    (Jsonb({"count": len(rows)}), operation),
                )
        finally:
            self.active.discard(task)
        fields = {
            "user_id",
            "nickname",
            "remark",
            "group_id",
            "group_name",
            "member_count",
            "card",
            "role",
        }
        return {
            "items": [{k: str(v)[:200] for k, v in r.items() if k in fields} for r in rows[:300]],
            "omitted": max(0, len(rows) - 300),
        }

    async def add_asset(self, name, encoded):
        name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
        ext = Path(name).suffix.lower()
        if not name or len(name) > 150 or ext not in FILES or len(encoded) > 28_000_000:
            raise ValueError("文件类型或大小不受支持（最大 20MB）")
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("文件编码无效") from exc
        if not 0 < len(data) <= 20 * 1024 * 1024:
            raise ValueError("文件大小需要在 1 字节～20MB 之间")
        asset = uuid.uuid4()
        path = self.root / (str(asset) + ext)
        path.write_bytes(data)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO qq_assets(id,name,path,size,kind) VALUES(%s,%s,%s,%s,%s)",
                (asset, name, str(path), len(data), "image" if ext in MEDIA else "file"),
            )
        return {"asset_id": str(asset), "name": name, "kind": "image" if ext in MEDIA else "file"}

    async def assets(self):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT id,name,size,kind,created_at FROM qq_assets ORDER BY created_at DESC LIMIT 100"
                )
            ).fetchall()

    async def send_media(self, asset_id, target_type, target_id, key, reply_to=""):
        settings, identity = await self.settings(), self.identity()
        if target_type not in ("private", "group") or not ID.fullmatch(str(target_id)):
            raise ValueError("收件人类型或账号无效")
        if target_type == "private" and target_id not in [
            str(identity.get("owner_qq", "")),
            *settings["private_ids"],
        ]:
            raise ValueError("私聊收件人未授权")
        if target_type == "group" and not group_permission(settings, target_id, "media"):
            raise ValueError("该群未获附件发送权限")
        async with self.pool.connection() as conn:
            asset = await (
                await conn.execute("SELECT * FROM qq_assets WHERE id=%s", (uuid.UUID(asset_id),))
            ).fetchone()
        if not asset:
            raise ValueError("文件不在已授权素材库中")
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("素材已不存在或路径越界")
        if not settings["send_images" if asset["kind"] == "image" else "send_files"]:
            raise ValueError("附件类型已被关闭")
        if target_type == "group":
            groups = await self.call("get_group_list")
            if not any(str(g.get("group_id")) == target_id for g in groups):
                raise ValueError("Bot 不在这个群内")
        if reply_to:
            async with self.pool.connection() as conn:
                valid = await (
                    await conn.execute(
                        "SELECT 1 FROM messages WHERE platform='aiocqhttp' AND event_id=%s AND ((chat_type='group' AND group_id=%s) OR (chat_type='private' AND conversation_id=%s)) LIMIT 1",
                        (
                            reply_to,
                            target_id,
                            str(identity.get("platform_id", "companion_qq"))
                            + ":FriendMessage:"
                            + target_id,
                        ),
                    )
                ).fetchone()
            if not valid or not re.fullmatch(r"-?[0-9]+", reply_to):
                raise ValueError("只能引用这个会话中真实存在的消息")
        segments = ([{"type": "reply", "data": {"id": reply_to}}] if reply_to else []) + [
            {"type": asset["kind"], "data": {"file": path.as_uri(), "name": asset["name"]}}
        ]
        action = "send_private_msg" if target_type == "private" else "send_group_msg"
        return await self.execute(
            action,
            {
                "user_id" if target_type == "private" else "group_id": int(target_id),
                "message": segments,
            },
            key,
            "bot",
            {
                "target_type": target_type,
                "target_id": target_id,
                "asset_id": str(asset["id"]),
                "asset_kind": asset["kind"],
                "name": asset["name"],
                "reply_to": reply_to,
            },
        )

    async def recall(self, operation_id, key):
        if not (await self.settings())["recall"]:
            raise ValueError("撤回权限已关闭")
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM qq_operations WHERE id=%s AND account_qq=%s",
                    (uuid.UUID(operation_id), str(self.identity().get("bot_qq", ""))),
                )
            ).fetchone()
        if (
            not row
            or row["action"] not in ("send_private_msg", "send_group_msg", "send_msg")
            or not row["message_id"]
        ):
            raise ValueError("只能撤回 Bot 确认发出的消息")
        result = await self.execute(
            "delete_msg",
            {"message_id": row["message_id"]},
            key,
            "bot",
            {"original_operation_id": str(row["id"]), "message_id": row["message_id"]},
        )
        if result["status"] in ("failed", "unknown"):
            for _ in range(8):
                async with self.pool.connection() as conn:
                    confirmed = await (
                        await conn.execute(
                            "SELECT recalled_at FROM qq_operations WHERE id=%s", (row["id"],)
                        )
                    ).fetchone()
                if confirmed["recalled_at"]:
                    async with self.pool.connection() as conn:
                        await conn.execute(
                            "UPDATE qq_operations SET status='sent',error=NULL,metadata=metadata||%s WHERE id=%s",
                            (Jsonb({"confirmed_by": "QQ_notice"}), result["operation_id"]),
                        )
                    result.update(status="sent", error=None, confirmed_by="QQ_notice")
                    break
                await asyncio.sleep(0.25)
        if result["status"] == "sent":
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE qq_operations SET recalled_at=now() WHERE id=%s", (row["id"],)
                )
        return result

    async def snapshot(self):
        async with self.pool.connection() as conn:
            requests = await (
                await conn.execute(
                    "SELECT id,account_qq,kind,sub_type,user_id,group_id,comment,status,created_at,decided_at FROM qq_requests ORDER BY created_at DESC LIMIT 50"
                )
            ).fetchall()
            operations = await (
                await conn.execute("SELECT * FROM qq_operations ORDER BY created_at DESC LIMIT 50")
            ).fetchall()
        return {
            "settings": await self.settings(),
            "requests": requests,
            "operations": operations,
            "assets": await self.assets(),
            "pending": sum(r["status"] == "pending" for r in requests),
        }

    async def close(self):
        self.closing = True
        self.uninstall_transport()
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
