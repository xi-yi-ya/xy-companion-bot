from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from local_web import LocalWebMiddleware


@pytest.fixture
def app():
    async def index(request):
        return HTMLResponse(
            '<div id="app"></div><script type="module" crossorigin src="/assets/main.js"></script>'
        )

    async def api(request):
        return JSONResponse({"authorization": request.headers.get("authorization")})

    inner = Starlette(routes=[Route("/", index), Route("/api/chat", api)])
    auth = SimpleNamespace(generate_jwt=lambda username, auth_source: "test-local-identity")
    server = SimpleNamespace(
        config={"dashboard": {"username": "owner"}},
        asgi_app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(auth=auth))),
    )
    return LocalWebMiddleware(inner, server, 6185)


async def test_local_no_account_and_entry_bootstrap(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:6185"
    ) as client:
        entry = await client.get("/")
        assert entry.status_code == 200
        assert (
            "/companion/bootstrap.js" in entry.text and 'data-entry="/assets/main.js"' in entry.text
        )
        response = await client.get("/api/chat")
        assert response.json()["authorization"] == "Bearer test-local-identity"
        session = await client.get("/companion/local-session")
        assert session.json()["username"] == "owner"
        assert session.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://unrelated.example"},
        {"host": "unrelated.example:6185"},
        {"sec-fetch-site": "cross-site"},
    ],
)
async def test_local_mode_rejects_foreign_origins_and_hosts(app, headers):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:6185"
    ) as client:
        response = await client.get("/companion/local-session", headers=headers)
        assert response.status_code == 403


async def test_local_mode_rejects_remote_peer(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=("192.168.1.10", 1234)),
        base_url="http://127.0.0.1:6185",
    ) as client:
        assert (await client.get("/")).status_code == 403
        assert (await client.get("/companion/qq")).status_code == 403


async def test_qq_login_page_is_local_and_does_not_contain_a_hardcoded_secret(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:6185"
    ) as client:
        page = await client.get("/companion/qq")
        assert page.status_code == 200 and "QQ 登录" in page.text
        assert "bind_command" in page.text and "setInterval" in page.text
        assert page.headers["cache-control"] == "no-store"


async def test_opaque_frame_exception_is_limited_to_read_only_scoped_assets(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:6185"
    ) as client:
        headers = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
        path = "/api/v1/plugins/test/views/settings/_t/scoped-token/app.js"
        assert (await client.get(path, headers=headers)).status_code == 404
        assert (await client.post(path, headers=headers)).status_code == 403
        assert (await client.get("/companion/local-session", headers=headers)).status_code == 403
