"""Message provenance, independent of the person owning the memory."""

import re


def event_source(event, role):
    private = event.is_private_chat()
    return {
        "platform": str(event.get_platform_name()),
        "platform_id": str(event.get_platform_id()),
        "chat_type": "private" if private else "group",
        "group_id": None if private else str(event.get_group_id()),
        "sender_id": str(event.get_sender_id() if role == "user" else event.get_self_id()),
    }


def message_source(platform, account, conversation, role, content="", source=None):
    """Infer legacy callers conservatively; actual events supply explicit metadata."""
    parts = str(conversation).split(":", 2)
    native = len(parts) == 3 and parts[1] in ("FriendMessage", "GroupMessage")
    room = str(platform).split(":")
    group = platform.startswith("qqgroup:") or (native and parts[1] == "GroupMessage")
    result = {
        "platform": "aiocqhttp" if platform.startswith("qqgroup:") else platform,
        "platform_id": parts[0] if native else None,
        "chat_type": "group" if group else "private",
        "group_id": room[2] if len(room) >= 3 and group else None,
        "sender_id": None,
    }
    if group:
        if platform.startswith("qqgroup:"):
            result["platform_id"] = room[1]
        if not result["group_id"] and native:
            match = re.search(r"(?:^|_)(\d+)$", parts[2])
            result["group_id"] = match[1] if match else None
        if role == "user":
            if not str(account).startswith("room:"):
                result["sender_id"] = str(account)
            else:
                match = re.match(r"^\[群成员 [^\n]*（QQ (\d+)） 发言\]\n", content)
                result["sender_id"] = match[1] if match else None
        elif str(account).startswith("room:"):
            result["sender_id"] = str(account)[5:]
    elif role == "user":
        result["sender_id"] = str(account)
    if source is not None:
        if set(source) - set(result):
            raise ValueError("未知的消息来源字段")
        result.update(source)
    if result["chat_type"] not in ("private", "group"):
        raise ValueError("chat_type 必须是 private 或 group")
    if result["chat_type"] == "private" and result["group_id"] is not None:
        raise ValueError("私聊不能包含 group_id")
    for key, value in result.items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"消息来源 {key} 必须是非空字符串或 NULL")
    return result
