import asyncio
import json

import httpx
import pytest

from plugin.web_access import WebAccess, WebTurn, public_url, validate_web_settings


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Users/private.txt",
        "http://127.0.0.1:6185",
        "https://192.168.1.2",
        "http://[::1]",
        "https://localhost",
        "https://host.internal/x",
        "https://u:password@example.com",
    ],
)
def test_public_page_rejects_files_credentials_and_internal_hosts(url):
    with pytest.raises(ValueError):
        public_url(url)


async def test_fixed_provider_endpoint_and_hard_per_turn_budget(store, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-credential")
    calls = []

    async def transport(request):
        assert request.url.host == "api.deepseek.com"
        assert request.headers["Authorization"] == "Bearer synthetic-test-credential"
        calls.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Official docs",
                            "url": "https://example.com/docs",
                            "summary": "参考信息" * 1000,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"title": "Documentation", "content": "网页正文" * 15000})

    web = WebAccess(store.pool, httpx.MockTransport(transport))
    await web.save_settings({"enabled": True, "max_calls": 2, "context_budget": 4000})
    msg = await store.record("validation", "web-test", "web-test", "first", "user", "查询官方文档")
    turn = WebTurn(2, 4000)
    results = await asyncio.gather(
        turn.run(web, msg["person_id"], msg["id"], "search", "official docs"),
        turn.run(web, msg["person_id"], msg["id"], "fetch", "https://example.com/docs"),
    )
    assert sum(len(result.encode()) for result in results) <= 4000
    assert all(isinstance(json.loads(result), dict) for result in results)
    denied = json.loads(await turn.run(web, msg["person_id"], msg["id"], "search", "again"))
    assert "error" in denied and len(calls) == 2
    assert len(await web.history(msg["person_id"])) == 2


async def test_disabled_setting_stops_existing_turn_and_provider_errors_are_sanitized(
    store, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-credential")

    async def transport(request):
        return httpx.Response(402, json={"error": "raw provider response must stay out of chat"})

    web = WebAccess(store.pool, httpx.MockTransport(transport))
    msg = await store.record("validation", "web-test", "web-test", "first", "user", "搜索")
    turn = WebTurn(3, 12000)
    result = json.loads(await turn.run(web, msg["person_id"], msg["id"], "search", "test"))
    assert "HTTP 402" in result["error"]
    assert "raw provider" not in json.dumps(result)
    await web.save_settings({"enabled": False, "max_calls": 3, "context_budget": 12000})
    assert (
        "关闭"
        in json.loads(await turn.run(web, msg["person_id"], msg["id"], "search", "test"))["error"]
    )
    assert len(await web.history(msg["person_id"])) == 1
    for changes in [{"enabled": 1}, {"max_calls": True}, {"context_budget": -1}]:
        with pytest.raises(ValueError):
            validate_web_settings({**await web.settings(), **changes})
