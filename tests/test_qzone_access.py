import httpx
import pytest
from test_qzone import NOW, Harness

from plugin.qzone import DEFAULT_QZONE, qzone_read_call
from plugin.qzone_http import USER_FEEDS, QzoneReader, QzoneReadError


async def test_manual_read_works_when_only_automatic_browsing_is_disabled(store):
    h = Harness(store)
    await store.qzone.save({**DEFAULT_QZONE, "enabled": False}, NOW)
    assert (await h.service.step())["status"] == "waiting"
    result = await h.service.step(force=True)
    assert result["status"] == "complete" and result["posts_data"] == h.posts


async def test_note_failure_does_not_hide_successfully_fetched_posts(store):
    h = Harness(store)

    async def invalid(posts):
        raise ValueError("invalid model JSON")

    h.service.review = invalid
    result = await h.service.step(force=True)
    assert result["status"] == "complete" and result["posts_data"] == h.posts
    assert result["reviewed"] == 0 and result["review_error"]
    state = (await store.qzone.snapshot())["state"]
    assert state["last_success"] == NOW and "阅读笔记" in state["last_error"]


async def test_empty_fetch_does_not_reprocess_old_failed_notes(store):
    h = Harness(store)

    async def invalid(posts):
        raise ValueError("invalid model JSON")

    h.service.review = invalid
    await h.service.step(force=True)

    async def unexpected(posts):
        raise AssertionError("空列表不能整理之前未完成的笔记")

    h.posts = []
    h.service.review = unexpected
    result = await h.service.step(force=True)
    assert (
        result["status"] == "complete" and not result["posts_data"] and not result["review_error"]
    )


def test_textual_read_recovery_does_not_execute_publication_or_other_tools():
    assert qzone_read_call(
        '<tool_call>{"name":"companion_qzone_read","arguments":{"target_qq":"777777","limit":3}}</tool_call>'
    ) == ("companion_qzone_read", {"target_qq": "777777", "limit": 3})
    assert qzone_read_call("普通回复") is None
    for name in ("companion_qzone_consider", "companion_qq_send_media", "anything"):
        with pytest.raises(ValueError):
            qzone_read_call(
                '<tool_call>{"name":"' + name + '","arguments":{}}</tool_call> companion_qzone_read'
            )


async def test_targeted_space_read_is_readonly_and_retains_real_author():
    calls = []

    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie"}

    async def handle(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url).startswith(USER_FEEDS)
        assert request.url.params["uin"] == "777777"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msglist": [
                    {
                        "tid": "test-target",
                        "content": "合成测试动态",
                        "created_time": int(NOW.timestamp()),
                    }
                ],
            },
        )

    account, posts = await QzoneReader(credentials, transport=httpx.MockTransport(handle)).fetch(
        "777777"
    )
    assert account == "123456" and len(calls) == 1
    assert len(posts) == 1 and posts[0]["author"] == "777777" and "合成测试" in posts[0]["body"]


async def test_targeted_reader_refusal_is_not_faked_as_empty_feed():
    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie"}

    async def handle(request):
        return httpx.Response(200, json={"code": -3000, "message": "not authorized"})

    with pytest.raises(QzoneReadError):
        await QzoneReader(credentials, transport=httpx.MockTransport(handle)).fetch("777777")
