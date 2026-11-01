"""Turn QQ quoted content into sourced external material, not user testimony."""

import copy
import json
import uuid

from .attachments import component_kind, prepare_attachments


async def prepare_quotes(event, reader, caption, fetch_link, *, allow_links=True):
    items = []
    components = []
    for component in event.get_messages():
        if component_kind(component) != "reply":
            components.append(component)
            continue
        quote_id = str(component.id)
        chain = list(getattr(component, "chain", None) or [])
        text = getattr(component, "message_str", None) or getattr(component, "text", None) or ""
        if not text:
            text = "".join(getattr(c, "text", "") for c in chain if component_kind(c) == "plain")
        proxy = copy.copy(event)
        proxy.message_obj = copy.copy(event.message_obj)
        proxy.message_obj.message = [c for c in chain if component_kind(c) != "reply"]
        proxy.message_obj.raw_message = {}
        proxy.message_str = ""  # Quoted URLs do not cause another automatic fetch.
        parsed = await prepare_attachments(
            proxy, reader, caption, fetch_link, allow_links=allow_links
        )
        body = {
            "quoted_message_id": quote_id,
            "speaker_id": str(getattr(component, "sender_id", "")),
            "speaker_name": str(getattr(component, "sender_nickname", "") or ""),
            "text": text,
            "materials": [
                {k: a.get(k) for k in ("kind", "name", "status", "text", "error")} for a in parsed
            ],
        }
        available = bool(text.strip()) or any(item["status"] == "ready" for item in parsed)
        if not available:
            body["error"] = "引用内容无法获取，不能猜测原消息"
        items.append(
            {
                "id": uuid.uuid4(),
                "kind": "quote",
                "name": f"引用消息 #{quote_id}",
                "status": "ready" if available else "error",
                "path": None,
                "text": json.dumps(body, ensure_ascii=False),
                "error": body.get("error"),
            }
        )
    event.message_obj.message = components
    return items
