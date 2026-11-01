"""Verified receipts and bounded recovery for persona calls emitted as plain text."""

import json
import re
from xml.etree import ElementTree

from .persona import L0_FIELDS, L1_FIELDS

PERSONA_TOOL_RULE = """提出具体人设修改时，必须实际调用 companion_propose_persona；使用原生函数调用，不能在普通回答里写 <tool_call>、JSON 或伪造工具结果。
参数为 layer、field、new_value、reason、quote（用户本轮某条原话），不要把 new_value 写成 change。
只有工具返回真实 proposal_id 和 pending 状态，才能说申请已进入待审，并告诉用户编号。blocked/stale/checking 分别如实说明，不能声称已生效。
用户让你重试先前的具体修改时，可以用本轮“再提一次”等原话作为请求上下文；理由不要编造用户的逐字确认。"""


def needs_verified_output(text):
    return bool(
        re.search(
            r"人设|个性|性格|习惯|申请|提交|再提|修改|直接提|你提|改一下|companion_propose_persona",
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
    return {
        "proposal_id": proposal["id"],
        "status": status,
        "applied": False,
        "instruction": labels.get(status, status),
        "check": proposal["check_result"],
        "layer": proposal["layer"],
        "field": proposal["field"],
        "new_value": proposal["new_value"],
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
            arguments[name] = "".join(parameter.itertext())
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
    source = quoted_source(sources, args.get("quote", ""))
    if source["person_id"] != "webchat:owner":
        raise ValueError("全局人设申请仅支持已绑定主人的私聊；群聊不能修改全局人设")
    target = args.get("target_trait_id", 0)
    target = int(target) if target not in (None, "") else 0
    proposal = await store.personas.propose(
        source["person_id"],
        layer,
        field,
        args.get("new_value", args.get("change")),
        args.get("reason"),
        [{"message_id": source["id"], "quote": args["quote"]}],
        check,
        target_id=target or None,
    )
    return receipt(proposal)


async def verify_reply(text, sources, store, check, actual_receipt=None):
    """Only recover the proposal tool: it creates an approval request, never applies it."""
    blocks = draft_blocks(text)
    if blocks and sources and sources[-1] and sources[-1]["person_id"] == "webchat:owner":
        user_text = "\n".join(r["content"] for r in sources if r)
        intent = bool(re.search(r"人设|个性|性格|习惯|申请|提交|再提|修改", user_text))
        if intent:
            if len(blocks) != 1:
                return "这轮人设申请格式不明确，尚未提交，也没有更改设定。", None
            try:
                call = json.loads(blocks[0])
                if call.get("name") != "companion_propose_persona":
                    return "这轮没有执行有效的人设申请工具，也没有更改设定。", None
                if actual_receipt:
                    result = actual_receipt
                else:
                    args = call["arguments"]
                    if not isinstance(args, dict):
                        raise ValueError("人设申请参数无效")
                    # The model's source claims are untrusted. Use a real current
                    # message when this legacy text draft omitted a valid quote.
                    try:
                        quoted_source(sources, args.get("quote", ""))
                    except ValueError:
                        args["quote"] = sources[-1]["content"]
                    result = await submit(store, sources, args, check)
                return describe(result), result
            except (ValueError, KeyError, TypeError):
                return "这轮人设申请参数校验未通过，尚未提交，也没有更改设定。", None
    if actual_receipt:
        # The actual persisted status wins over a model claiming a successful
        # approval after the tool merely created a pending request.
        return describe(actual_receipt), actual_receipt
    if re.search(
        r"(?:已|已经|刚刚|刚才).{0,5}提交|提交(?:好了|成功)|申请.{0,3}(?:提交了|提交成功|已提交)",
        text,
    ) and re.search(r"人设|个性|性格|习惯|设定|申请|L[01]", text):
        ids = {int(value) for value in re.findall(r"#(\d+)", text)}
        if ids and sources and sources[-1]:
            proposals = await store.personas.proposals(sources[-1]["person_id"])
            existing = next((p for p in proposals if p["id"] in ids), None)
            if existing:
                result = receipt(existing)
                return describe(result), result
        return "这轮没有取得人设申请的入库回执，不能确认已经提交；当前设定没有更改。", None
    return text, None
