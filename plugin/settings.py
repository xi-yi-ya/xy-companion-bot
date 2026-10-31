"""Editable companion persona and context budgets."""

DEFAULT_SETTINGS = {
    "bot_name": "小伴",
    "persona": "你是用户的日常陪伴伙伴。用自然的简体中文闲聊，认真倾听，简短回应，适时追问。\n不需要每轮列清单或总结，不假装拥有现实身体、已做过的事情或不存在的共同经历。",
    "memory_budget": 30000,
    "history_budget": 240000,
}


def validate_settings(payload):
    if not isinstance(payload, dict) or set(payload) != set(DEFAULT_SETTINGS):
        raise ValueError("设置字段不完整")
    result = dict(payload)
    for key, label, limit in [("bot_name", "名字", 40), ("persona", "人设", 30000)]:
        if not isinstance(result[key], str) or not 1 <= len(result[key].strip()) <= limit:
            raise ValueError(f"{label}需要 1～{limit} 个字符")
        result[key] = result[key].strip()
    for key, label, lower, upper in [
        ("memory_budget", "记忆预算", 200, 60000),
        ("history_budget", "近期对话预算", 500, 360000),
    ]:
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError(f"{label}需要是 {lower}～{upper} 的整数")
    return result
