"""Verified receipts and bounded recovery for persona calls emitted as plain text."""

import json
import logging
import re
from xml.etree import ElementTree

from .persona import L0_FIELDS, L1_FIELDS

logger = logging.getLogger(__name__)

PERSONA_TOOL_RULE = """提出具体人设修改时，必须实际调用 companion_propose_persona；使用原生函数调用，不能在普通回答里写 <tool_call>、JSON 或伪造工具结果。
你可以基于相处中的思考主动申请调整自己的性格或表达，不需要先征求“能否提出申请”，也不要求用户本轮先说“修改”。只在有明确必要时申请，不频繁重复提交。
自主发起时设置 self_initiated=true，写出具体 new_value 和自己的 reason，quote 可留空；程序会关联真实对话背景，背景不是用户要求或批准，不要编造用户的授权原话。此类申请始终等待主人手动批准。
回应用户明确修改要求时 self_initiated=false，quote 引用用户本轮真实原话。共同参数为 layer、field、new_value、reason，不要把 new_value 写成 change。
普通回答里的草稿不等于提交；未取得回执时应明确说尚未提交，不承诺后台已有申请。
只有工具返回真实 proposal_id 和 pending 状态，才能说申请已进入待审，并告诉用户编号；approved 且 applied=true 才能说已按授权生效。blocked/stale/checking 分别如实说明。
用户让你重试先前的具体修改时，可以用本轮“再提一次”等原话作为请求上下文；理由不要编造用户的逐字确认。"""


def needs_verified_output(text):
    return bool(
        re.search(
            r"人设|个性|性格|习惯|申请|提交|再提|修改|直接提|你提|改一下|都加|加上|就加|加进去|提出来|提一下|就提|提吧|同意|companion_propose_persona",
            text,
        )
    )


def sources_for(event):
    batch = event.get_extra("companion_batch")
    return [e.row for e in batch] if batch else [event.get_extra("companion_message")]


def quoted_source(sources, quote):
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("需要引用本轮用户的一条真实原话")
    row = next((r for r in reversed(sources) if r and quote in r["content"]), None)
    if row is None:
        raise ValueError("申请来源必须逐字对应本轮用户消息，不能引用附件或 Bot 自己的话")
    return row


def receipt(proposal):
    status = proposal["status"]
    labels = {
        "pending": "已进入待审，请在 Bot 设置 → 修改审批中手动批准；尚未生效",
        "checking": "正在检查，尚未进入待审",
        "blocked": "检查未通过，请在修改审批中查看原因；没有生效",
        "stale": "申请已过期，需要重新检查",
        "rejected": "申请已被拒绝，没有生效",
        "approved": "该申请此前已由用户批准，本轮未重复修改",
    }
    if status == "blocked" and proposal["check_result"].get("retryable"):
        labels["blocked"] = "检查暂时失败，尚未进入待审；" + (
            "已达 3 次检查上限，请先排查服务错误"
            if proposal["check_result"].get("attempts", 1) >= 3
            else "可在 Bot 设置 → 修改审批中重新检查"
        )
    automatic = status == "approved" and proposal.get("approved_by") == "system_auto"
    return {
        "proposal_id": proposal["id"],
        "status": status,
        "applied": automatic,
        "instruction": "已按你开启的自动模式通过一致性检查并生效"
        if automatic
        else labels.get(status, status),
        "check": proposal["check_result"],
        "layer": proposal["layer"],
        "field": proposal["field"],
        "new_value": proposal["new_value"],
        "self_initiated": proposal.get("origin") == "bot_self_request",
    }


def describe(result):
    return f"人设修改申请 #{result['proposal_id']}：{result['instruction']}。"


def draft_blocks(text):
    blocks = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, flags=re.S)
    if blocks or "DSML" not in text:
        return blocks
    # Some Flash replies leak the provider's textual DSML form instead of an
    # SDK tool_call. Recover only a single approval-request draft, never an
    # arbitrary function or executable expression.
    normalized = re.sub(r"([<]/?)[|｜]{1,2}DSML[|｜]{1,2}\s*", r"\1", text)
    fragment = re.search(r"<calls>.*?</calls>", normalized, re.S)
    if not fragment or len(fragment[0]) > 64000:
        return ["invalid-draft"]
    try:
        root = ElementTree.fromstring(fragment[0])
        if len(root) != 1 or root[0].tag != "invoke":
            return ["invalid-draft"]
        invoke = root[0]
        arguments = {}
        for parameter in invoke:
            name = parameter.attrib.get("name")
            if parameter.tag != "parameter" or not name or name in arguments:
                return ["invalid-draft"]
            value = "".join(parameter.itertext())
            if name == "self_initiated" and parameter.attrib.get("string") == "false":
                if value.strip() not in ("true", "false"):
                    return ["invalid-draft"]
                arguments[name] = value.strip() == "true"
            else:
                arguments[name] = value
        return [
            json.dumps(
                {"name": invoke.attrib.get("name"), "arguments": arguments}, ensure_ascii=False
            )
        ]
    except ElementTree.ParseError:
        return ["invalid-draft"]


async def submit(store, sources, arguments, check):
    args = dict(arguments)
    field = args.get("field")
    layer = args.get("layer")
    if not layer:
        layer = "L1" if field in L1_FIELDS else "L0" if field in L0_FIELDS else ""
    self_initiated = args.get("self_initiated", False)
    if type(self_initiated) is not bool:
        raise ValueError("self_initiated 必须是布尔值")
    quote = args.get("quote", "")
    if self_initiated and (quote is None or quote == ""):
        source = next((r for r in reversed(sources) if r), None)
        if not source or source.get("role") != "user":
            raise ValueError("自主申请需要真实的主人对话背景，不能伪造用户消息")
        quote = source["content"][:2000]
    else:
        source = quoted_source(sources, quote)
    if source["person_id"] != "webchat:owner" or source.get("chat_type", "private") != "private":
        raise ValueError("全局人设申请仅支持已绑定主人的私聊；群聊不能修改全局人设")
    target = args.get("target_trait_id", 0)
    target = int(target) if target not in (None, "") else 0
    proposal = await store.personas.propose(
        source["person_id"],
        layer,
        field,
        args.get("new_value", args.get("change")),
        args.get("reason"),
        [{"message_id": source["id"], "quote": quote}],
        check,
        origin="bot_self_request" if self_initiated else "bot_request",
        target_id=target or None,
    )
    return receipt(proposal)


def prose_draft(text):
    """Only a concrete, single quoted persona draft with an immediate submit claim."""
    if "```" in text or re.search(r"示例|举例|假设|假如|先不提交|暂不提交|不申请|不提交", text):
        return None
    if not re.search(r"(?:我.{0,8}(?:提一个|提一条|提交|提出|申请))", text):
        return None
    if not re.search(r"L[01]|人设|性格|长期习惯|说话习惯", text):
        return None
    blocks = re.findall(r"(?m)^>[^\n]*(?:\n>[^\n]*)*", text)
    if len(blocks) != 1:
        return None
    value = "\n".join(re.sub(r"^>\s?", "", line) for line in blocks[0].splitlines()).strip()
    return value if 1 <= len(value) <= 4000 else None


def recovery_is_discussion(text):
    # Deliberately bounded: “不用解释，直接提交” is not a veto.
    return bool(
        re.search(
            r"不要执行|不要提交|别提交|不要修改|别修改|暂不提交|先不提交|"
            r"只讨论|仅讨论|只展示|只解释|请解释|解释一下|举个例子|给个示例",
            text,
        )
    )


def text_arguments(blocks):
    """Normalize legacy text formats once; never treat arbitrary JSON as a call."""
    if len(blocks) != 1:
        raise ValueError("申请格式不明确")
    call = json.loads(blocks[0])
    if not isinstance(call, dict):
        raise ValueError("申请必须是对象")
    if call.get("name") != "companion_propose_persona":
        raise ValueError("没有执行有效的人设申请工具")
    args = call.get("arguments")
    if not isinstance(args, dict):
        raise ValueError("申请参数必须是对象")
    return dict(args)


async def verify_reply(text, sources, store, check, actual_receipt=None, extract_draft=None):
    """One recovery pipeline; persisted receipts always precede text heuristics."""
    if actual_receipt:
        return describe(actual_receipt), actual_receipt
    blocks, draft = draft_blocks(text), prose_draft(text)
    owner = bool(
        sources
        and sources[-1]
        and sources[-1]["person_id"] == "webchat:owner"
        and sources[-1].get("chat_type", "private") == "private"
    )
    if owner and (blocks or draft):
        user_text = "\n".join(r["content"] for r in sources if r)
        if recovery_is_discussion(user_text):
            return text, None
        try:
            if blocks:
                args = text_arguments(blocks)
                if args.get("self_initiated") is not True:
                    try:
                        quoted_source(sources, args.get("quote", ""))
                    except ValueError:
                        if not needs_verified_output(user_text):
                            return "这只是人设草稿，尚未提交，也没有更改设定。", None
                        args["quote"] = sources[-1]["content"]
            else:
                if extract_draft is None:
                    raise ValueError("没有草稿解析器")
                args = await extract_draft(text, draft)
                if not isinstance(args, dict) or args.get("new_value") != draft:
                    raise ValueError("只能恢复原文中的确切草稿")
                args = {
                    k: args[k]
                    for k in ("layer", "field", "new_value", "reason", "target_trait_id")
                    if k in args
                }
                args["self_initiated"] = True
            result = await submit(store, sources, args, check)
            return describe(result) + (
                "\n拟修改为：" + draft if draft and not blocks else ""
            ), result
        except (ValueError, KeyError, TypeError) as exc:
            return f"这轮人设草稿校验未通过（{exc}），尚未提交，也没有更改设定。", None
        except Exception as exc:
            logger.warning("Persona recovery failed: %s", type(exc).__name__)
            # A database write may already have committed: do not claim absence.
            return (
                "本轮未取得人设申请回执，无法确认提交状态，请在 Bot 设置 → 修改审批中查看。",
                None,
            )
    if re.search(
        r"(?:已|已经|刚刚|刚才).{0,5}提交|提交(?:好了|成功)|申请.{0,3}(?:提交了|提交成功|已提交)",
        text,
    ) and re.search(r"人设|个性|性格|习惯|设定|申请|L[01]", text):
        ids = {int(value) for value in re.findall(r"#(\d+)", text)}
        if ids and owner:
            proposals = await store.personas.proposals(sources[-1]["person_id"])
            existing = next((p for p in proposals if p["id"] in ids), None)
            if existing:
                result = receipt(existing)
                return describe(result), result
        return "这轮没有取得人设申请的入库回执，不能确认已经提交；当前设定没有更改。", None
    return text, None


async def recover_stored(store, assistant_id, check, extract_draft=None):
    """Repair a persisted, owner-scoped tool draft using its actual user input."""
    async with store.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM messages WHERE id=%s AND role='assistant' AND person_id='webchat:owner' AND chat_type='private'",
                (int(assistant_id),),
            )
        ).fetchone()
        if not row or (
            "companion_propose_persona" not in row["content"] and not prose_draft(row["content"])
        ):
            raise ValueError("没有找到主人私聊中的人设申请草稿")
        source_event_id = re.sub(r":bubble:\d+$", "", row["event_id"])
        source = await (
            await conn.execute(
                "SELECT * FROM messages WHERE conversation_id=%s AND event_id=%s AND role='user' AND person_id='webchat:owner' AND id<%s ORDER BY id DESC LIMIT 1",
                (row["conversation_id"], source_event_id, row["id"]),
            )
        ).fetchone()
    if not source:
        raise ValueError("没有找到这次申请对应的真实用户输入")
    text, result = await verify_reply(
        row["content"], [source], store, check, extract_draft=extract_draft
    )
    if not result:
        raise ValueError("草稿未通过来源或参数验证：" + text[:200])
    return {"reply": text, "receipt": result, "source_message_id": source["id"]}
