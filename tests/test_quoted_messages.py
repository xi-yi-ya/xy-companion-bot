import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from plugin.attachments import AttachmentReader, save_attachments
from plugin.quoted_messages import prepare_quotes


def components(monkeypatch):
    module = ModuleType("astrbot.api.message_components")
    module.Plain = lambda text: SimpleNamespace(type="plain", text=text)
    module.Image = SimpleNamespace(
        fromFileSystem=lambda path: SimpleNamespace(type="image", file=path, url=path)
    )
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", module)


def event(quote):
    value = SimpleNamespace(
        message_str="引用这个",
        get_platform_name=lambda: "webchat",
        message_obj=SimpleNamespace(message=[quote]),
    )
    value.get_messages = lambda: value.message_obj.message
    return value


async def reject(*args):
    raise AssertionError("Unexpected external request")


async def test_quote_keeps_speaker_and_content_out_of_user_testimony(store, tmp_path, monkeypatch):
    components(monkeypatch)
    e = event(
        SimpleNamespace(
            type="Reply",
            id="older",
            sender_id="other",
            sender_nickname="群友",
            message_str="我是医生，代号 QUOTE-713",
            chain=[],
        )
    )
    items = await prepare_quotes(e, AttachmentReader(tmp_path), reject, reject)
    body = json.loads(items[0]["text"])
    assert body["speaker_id"] == "other" and "QUOTE-713" in body["text"]
    assert e.message_obj.message == []  # Native collection will not fetch it again.
    msg = await store.record("webchat", "owner", "quoted", "one", "user", "引用这个")
    await save_attachments(store.pool, msg["id"], items)
    later = await store.record("webchat", "owner", "quoted", "two", "user", "代号是什么")
    _, history = await store.context(msg["person_id"], later)
    assert "QUOTE-713" in history[0]["content"]
    with pytest.raises(ValueError):
        await store.confirm(msg["person_id"], 123, msg["id"], "我是医生")
    async with store.pool.connection() as c:
        raw = await (
            await c.execute("SELECT content FROM messages WHERE id=%s", (msg["id"],))
        ).fetchone()
    assert raw["content"] == "引用这个"


async def test_quoted_image_uses_existing_reader_and_caption(tmp_path, monkeypatch):
    components(monkeypatch)
    path = tmp_path / "quote.png"
    Image.new("RGB", (20, 20), "blue").save(path)
    image = SimpleNamespace(type="image", file=str(path), url=str(path))
    e = event(
        SimpleNamespace(
            type="Reply", id="image-old", sender_id="123", message_str="", chain=[image]
        )
    )

    async def caption(path):
        return "蓝色图片中的代号 IMG-781"

    items = await prepare_quotes(e, AttachmentReader(tmp_path / "cache"), caption, reject)
    body = json.loads(items[0]["text"])
    assert body["materials"][0]["status"] == "ready" and "IMG-781" in body["materials"][0]["text"]


async def test_unavailable_quote_is_explicit(tmp_path, monkeypatch):
    components(monkeypatch)
    e = event(SimpleNamespace(type="Reply", id="missing", message_str="", chain=None))
    item = (await prepare_quotes(e, AttachmentReader(tmp_path), reject, reject))[0]
    assert item["status"] == "error" and "不能猜测" in item["error"]
