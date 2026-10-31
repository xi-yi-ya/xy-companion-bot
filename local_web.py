"""Loopback-only, account-free access and a small AstrBot WebUI extension."""

import asyncio
import os
import re
from pathlib import Path

from starlette.responses import FileResponse, JSONResponse

from local_instance import instance_id

WEB = Path(__file__).resolve().parent / "web"
ENTRY = re.compile(
    r'<script\b(?=[^>]*\btype=["\']module["\'])(?=[^>]*\bsrc=["\']([^"\']+)["\'])[^>]*>\s*</script>',
    re.I,
)


class LocalWebMiddleware:
    def __init__(self, app, server, port):
        self.app = app
        self.server = server
        self.port = port
        self.instance_id = os.environ.get("COMPANION_INSTANCE_ID") or instance_id(
            Path(__file__).parent
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        origins = {f"http://{host}" for host in hosts}
        peer = scope.get("client") or ("", 0)
        path = scope["path"]
        # Sandboxed plugin frames have an opaque origin. Their read-only assets
        # still require AstrBot's scoped path/query tokens in the inner router.
        sandbox_asset = scope.get("method") in ("GET", "HEAD") and (
            re.fullmatch(r"/api/v1/plugins/[\w-]+/views/[\w-]+/_t/[^/]+/.*", path)
            or path == "/api/plugin/page/bridge-sdk.js"
        )
        allowed = (
            peer[0] in ("127.0.0.1", "::1")
            and headers.get("host", "").lower() in hosts
            and (not headers.get("origin") or headers["origin"] in origins or sandbox_asset)
            and (headers.get("sec-fetch-site") != "cross-site" or sandbox_asset)
        )
        if not allowed:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return await JSONResponse({"error": "Local browser access only"}, status_code=403)(
                scope, receive, send
            )
        username = self.server.config["dashboard"]["username"]
        token = self.server.asgi_app.state.services.auth.generate_jwt(username, auth_source="local")
        # Preserve the existing owner identity, including all chat sessions and memories.
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
        scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
        if path == "/companion/local-session" and scope["type"] == "http":
            return await JSONResponse(
                {"username": username, "token": token}, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
        if path == "/companion/shutdown" and scope["type"] == "http":
            if scope.get("method") != "POST":
                return await JSONResponse({"error": "POST required"}, status_code=405)(
                    scope, receive, send
                )
            if headers.get("x-companion-instance") != self.instance_id:
                return await JSONResponse(
                    {"error": "Installation identity required"}, status_code=403
                )(scope, receive, send)
            core = self.server.core_lifecycle
            if not getattr(core, "_companion_shutdown_task", None):

                async def stop_after_response():
                    await asyncio.sleep(0.2)
                    await core.stop()

                core._companion_shutdown_task = asyncio.create_task(stop_after_response())
            return await JSONResponse({"accepted": True})(scope, receive, send)
        if path in ("/companion/bootstrap.js", "/companion/shell.css") and scope["type"] == "http":
            return await FileResponse(
                WEB / path.rsplit("/", 1)[1], headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
        entry = scope["type"] == "http" and (
            path in ("/", "/index.html")
            or (
                "text/html" in headers.get("accept", "")
                and not path.startswith(("/api", "/companion"))
            )
        )
        if not entry:
            return await self.app(scope, receive, send)
        # Rewrite only the SPA entry document, and keep APIs/SSE streaming unchanged.
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"accept-encoding"]
        start, chunks = None, []

        async def rewrite(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            content_type = dict(start["headers"]).get(b"content-type", b"")
            if start["status"] == 200 and b"text/html" in content_type:
                html = body.decode("utf-8")
                match = ENTRY.search(html)
                if match and '<div id="app"' in html:
                    tag = f'<script type="module" src="/companion/bootstrap.js" data-entry="{match.group(1)}"></script><link rel="stylesheet" href="/companion/shell.css">'
                    body = (html[: match.start()] + tag + html[match.end() :]).encode("utf-8")
            start["headers"] = [
                (k, v) for k, v in start["headers"] if k.lower() not in (b"content-length", b"etag")
            ]
            start["headers"].append((b"content-length", str(len(body)).encode()))
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, rewrite)


def keep_shutdown_alive(lifecycle_class):
    """Let native cleanup finish after its main task group has been cancelled."""
    if getattr(lifecycle_class, "_companion_stop_installed", False):
        return
    original_start = lifecycle_class.start

    async def start(core, *args, **kwargs):
        try:
            return await original_start(core, *args, **kwargs)
        finally:
            shutdown = getattr(core, "_companion_shutdown_task", None)
            if shutdown is not None and shutdown is not asyncio.current_task():
                await shutdown

    lifecycle_class.start = start
    lifecycle_class._companion_stop_installed = True


def install_local_web(port):
    """Attach the local adapter without editing the pinned AstrBot checkout."""
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.dashboard.server import AstrBotDashboard

    keep_shutdown_alive(AstrBotCoreLifecycle)

    original = AstrBotDashboard.__init__

    def initialize(server, *args, **kwargs):
        original(server, *args, **kwargs)
        if server.config["dashboard"]["host"] != "127.0.0.1":
            raise RuntimeError("Account-free mode requires dashboard.host=127.0.0.1")
        server.asgi_app.add_middleware(LocalWebMiddleware, server=server, port=port)

    AstrBotDashboard.__init__ = initialize
