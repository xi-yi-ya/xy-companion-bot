import json
import sys
import zipfile
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from PIL import Image

from plugin.attachments import (
    AttachmentReader,
    parse_document,
    prepare_attachments,
    save_attachments,
)


def office(path, member, xml):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, xml)
    return path


def docx(path, text="The document code is ORBIT-7319."):
    return office(
        path,
        "word/document.xml",
        f'<w:document xmlns:w="word"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>table-cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>',
    )


def test_docx_paragraph_table_and_header(tmp_path):
    path = docx(tmp_path / "draft.docx")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "word/header1.xml",
            '<w:hdr xmlns:w="word"><w:p><w:r><w:t>header-text</w:t></w:r></w:p></w:hdr>',
        )
    result = parse_document(path, path.name)
    assert "ORBIT-7319" in result and "table-cell" in result and "header-text" in result


def test_xlsx_keeps_sheets_values_and_formulas(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "预算"
    book.active.append(["项目", "数额"])
    book.active.append(["猫粮", 120])
    book.active.append(["总计", "=SUM(B2:B2)"])
    path = tmp_path / "test.xlsx"
    book.save(path)
    result = parse_document(path, path.name)
    assert "预算" in result and "猫粮\t120" in result and "=SUM" in result


def test_pptx_and_text_encodings(tmp_path):
    path = office(
        tmp_path / "test.pptx",
        "ppt/slides/slide1.xml",
        '<a:sld xmlns:a="slides"><a:p><a:r><a:t>slide-secret</a:t></a:r></a:p></a:sld>',
    )
    assert "slide-secret" in parse_document(path, path.name)
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        path = tmp_path / "note.txt"
        path.write_bytes("链接与附件测试".encode(encoding))
        assert parse_document(path, path.name) == "链接与附件测试"


def test_pdf_text_and_scan_notice(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (PDF-MARKER-9217) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "text.pdf"
    writer.write(path)
    assert "PDF-MARKER-9217" in parse_document(path, path.name)
    blank = PdfWriter()
    blank.add_blank_page(width=100, height=100)
    blank.write(path)
    with pytest.raises(ValueError, match="扫描 PDF"):
        parse_document(path, path.name)


async def no_caption(path):
    raise AssertionError("Document parser must not call a vision model")


async def test_cached_document_and_persistent_context_do_not_become_user_facts(store, tmp_path):
    path = docx(tmp_path / "plan.docx")
    reader = AttachmentReader(tmp_path / "cache")
    item = await reader.read(path.name, [str(path)], no_caption)
    assert item["status"] == "ready" and item["path"] != str(path)
    message = await store.record("webchat", "owner", "test:upload", "one", "user", "看看这个文件")
    await save_attachments(store.pool, message["id"], [item])
    await save_attachments(store.pool, message["id"], [item])
    async with store.pool.connection() as conn:
        raw = await (
            await conn.execute("SELECT content FROM messages WHERE id=%s", (message["id"],))
        ).fetchone()
        count = await (
            await conn.execute("SELECT count(*) AS n FROM message_attachments")
        ).fetchone()
    assert raw["content"] == "看看这个文件" and count["n"] == 1
    next_message = await store.record(
        "webchat", "owner", "test:upload", "two", "user", "刚才的代号是什么"
    )
    _, history = await store.context(message["person_id"], next_message)
    assert "ORBIT-7319" in history[-1]["content"]
    found = await store.search_history(message["person_id"], "ORBIT-7319")
    assert found and "ORBIT-7319" in found[0]["content"]
    assert await store.memories(message["person_id"]) == []


async def test_same_name_files_remain_distinct_and_saving_is_idempotent(store, tmp_path):
    reader = AttachmentReader(tmp_path / "cache")
    first = await reader.read(
        "same.docx", [str(docx(tmp_path / "a.docx", "FIRST-CODE"))], no_caption
    )
    second = await reader.read(
        "same.docx", [str(docx(tmp_path / "b.docx", "SECOND-CODE"))], no_caption
    )
    msg = await store.record("webchat", "owner", "test:same", "one", "user", "两个同名文件")
    for _ in range(2):
        await save_attachments(store.pool, msg["id"], [first, second])
    async with store.pool.connection() as conn:
        n = await (await conn.execute("SELECT count(*) AS n FROM message_attachments")).fetchone()
        context = await (
            await conn.execute("SELECT content FROM message_context WHERE id=%s", (msg["id"],))
        ).fetchone()
    assert (
        n["n"] == 2 and "FIRST-CODE" in context["content"] and "SECOND-CODE" in context["content"]
    )


async def test_qq_https_fallback_is_bounded_and_failures_are_explained(tmp_path):
    source = docx(tmp_path / "draft.docx").read_bytes()
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        if request.url.scheme == "https":
            raise httpx.RemoteProtocolError("Server disconnected")
        return httpx.Response(200, content=source)

    reader = AttachmentReader(tmp_path / "cache", httpx.MockTransport(handler))
    item = await reader.read("draft.docx", ["http://files.qq.com/test"], no_caption)
    assert item["status"] == "ready" and "ORBIT-7319" in item["text"]
    assert [httpx.URL(url).scheme for url in calls] == ["https", "http"]
    broken = await reader.read("draft.docx", ["https://files.qq.com/test"], no_caption)
    assert broken["status"] == "error" and "重新发送" in broken["error"]


async def test_private_redirect_and_oversized_download_rejected(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    reader = AttachmentReader(tmp_path / "cache", httpx.MockTransport(handler))
    result = await reader.read("a.txt", ["https://example.org/file"], no_caption)
    assert result["status"] == "error" and calls == ["example.org"]
    reader.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-length": str(33 * 1024 * 1024)})
    )
    result = await reader.read("a.txt", ["https://example.org/file"], no_caption)
    assert result["status"] == "error" and "32 MB" in result["error"]


async def test_napcat_c2c_signed_download_uses_qq_https_endpoint(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request.url)
        assert request.url.scheme == "https" and request.url.host == "grouptalk.c2c.qq.com"
        assert request.url.params["rkey"] == "opaque-test-value"
        return httpx.Response(200, content=b"downloaded")

    reader = AttachmentReader(tmp_path / "cache", httpx.MockTransport(handler))
    path = await reader.materialize(
        ["http://43.129.139.192/qqdownloadftnv5/test?rkey=opaque-test-value"],
        "test.txt",
        qq_cdn=True,
    )
    assert path.read_bytes() == b"downloaded" and len(calls) == 1


async def test_file_sent_image_is_recognized_and_captioned(tmp_path):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (80, 80), "red").save(path)
    seen = []

    async def caption(path):
        seen.append(path)
        return "红色图片"

    result = await AttachmentReader(tmp_path / "cache").read(path.name, [str(path)], caption)
    assert result["kind"] == "image" and result["text"] == "红色图片" and len(seen) == 1


def fake_components(monkeypatch):
    module = ModuleType("astrbot.api.message_components")
    module.Plain = lambda text: SimpleNamespace(type="plain", text=text)
    module.Image = SimpleNamespace(
        fromFileSystem=lambda path: SimpleNamespace(type="image", file=path, url=path)
    )
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", module)


async def test_web_and_qq_prepare_share_parser_and_replace_remote_components(tmp_path, monkeypatch):
    fake_components(monkeypatch)
    path = docx(tmp_path / "shared.docx")
    native_calls = []

    async def native(action, **params):
        native_calls.append((action, params))
        return {"file": str(path)}

    async def no_fetch(url):
        raise AssertionError("No link")

    for platform in ("webchat", "aiocqhttp"):
        comp = SimpleNamespace(
            type="file",
            name=path.name,
            file_=(str(path) if platform == "webchat" else ""),
            url="http://files.qq.com/expired",
        )
        event = SimpleNamespace(
            message_str="",
            get_platform_name=lambda: platform,
            bot=SimpleNamespace(call_action=native),
            message_obj=SimpleNamespace(
                self_id="456",
                message=[comp],
                raw_message={
                    "message": [
                        {"type": "file", "data": {"file": path.name, "file_id": "qq-file-1"}}
                    ]
                },
            ),
        )
        items = await prepare_attachments(
            event,
            AttachmentReader(
                tmp_path / "cache", httpx.MockTransport(lambda r: httpx.Response(404))
            ),
            no_caption,
            no_fetch,
        )
        assert items[0]["status"] == "ready" and "ORBIT-7319" in items[0]["text"]
        assert [c.type for c in event.message_obj.message] == ["plain"]
    assert native_calls == [("get_file", {"self_id": "456", "file_id": "qq-file-1"})]


async def test_links_and_qq_cards_use_existing_web_tool_budget(tmp_path, monkeypatch):
    fake_components(monkeypatch)
    calls = []

    async def fetch(url):
        calls.append(url)
        return json.dumps({"content": "真实网页内容", "external_data": True})

    event = SimpleNamespace(
        message_str="看看 https://example.org/article",
        get_platform_name=lambda: "webchat",
        message_obj=SimpleNamespace(
            message=[
                SimpleNamespace(
                    type="json", data={"meta": {"news": {"jumpUrl": "https://example.org/article"}}}
                )
            ]
        ),
    )
    items = await prepare_attachments(
        event, AttachmentReader(tmp_path / "cache"), no_caption, fetch
    )
    assert calls == ["https://example.org/article"] and items[0]["kind"] == "link"
    assert "真实网页内容" in items[0]["text"]


@pytest.mark.parametrize("name", ["old.doc", "old.xls", "old.ppt", "program.exe", "broken.docx"])
async def test_unreadable_files_return_explicit_status(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not-a-document")
    result = await AttachmentReader(tmp_path / "cache").read(name, [str(path)], no_caption)
    assert result["status"] == "error" and result["error"]
