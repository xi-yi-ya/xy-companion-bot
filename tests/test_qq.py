import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from plugin.qq import OWNER, QQBridge, configure_platform, guard_waking_stage


@pytest.mark.parametrize("outcome", ["ignore", "paired", "error", "allow"])
async def test_qq_gate_runs_before_any_native_command_and_fails_closed(outcome):
    reached, sent, stopped = [], [], []

    class Stage:
        async def process(self, event):
            reached.append(True)

    async def authorize(*args):
        if outcome == "error":
            raise RuntimeError("database unavailable")
        return outcome

    async def send(value):
        sent.append(value)

    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        is_private_chat=lambda: True,
        get_sender_id=lambda: "12345678",
        message_str="/help",
        stop_event=lambda: stopped.append(True),
        send=send,
        plain_result=lambda s: s,
    )
    guard_waking_stage(Stage, SimpleNamespace(authorize=authorize))
    guard_waking_stage(Stage, SimpleNamespace(authorize=authorize))
    await Stage().process(event)
    assert bool(reached) == (outcome == "allow")
    assert bool(sent) == (outcome == "paired")
    assert bool(stopped) == (outcome != "allow")


def settings(folder):
    value = {
        "enabled": True,
        "webui_port": 6099,
        "ws_port": 6199,
        "webui_token": "local-web-test",
        "ws_token": "local-ws-test",
        "bind_code": "PAIR12345678",
        "owner_qq": "",
    }
    (folder / "settings.json").write_text(json.dumps(value), "utf-8")
    return value


def test_qq_platform_is_local_idempotent_and_preserves_other_adapters(tmp_path):
    s = settings(tmp_path)
    existing = {"id": "existing", "type": "webchat", "enable": True}
    config = {"platform": [existing]}
    configure_platform(config, tmp_path)
    configure_platform(config, tmp_path)
    assert config["platform"][0] == existing and len(config["platform"]) == 2
    adapter = config["platform"][1]
    assert adapter["ws_reverse_host"] == "127.0.0.1"
    assert adapter["ws_reverse_token"] == s["ws_token"]


@pytest.mark.parametrize("leading_slash", [True, False])
async def test_explicit_pairing_shares_web_identity_and_only_allows_owner(
    store, tmp_path, leading_slash
):
    settings(tmp_path)
    bridge = QQBridge(tmp_path, store.pool)
    assert await bridge.authorize("12345678", "你好") == "ignore"
    assert await bridge.authorize("12345678", "/绑定 bad-code") == "ignore"
    command = bridge.snapshot()["bind_command"]
    assert await bridge.authorize("12345678", command if leading_slash else command[1:]) == "paired"
    assert bridge.snapshot()["bind_command"] is None
    assert await bridge.authorize("12345678", "你好") == "allow"
    assert await bridge.authorize("87654321", command) == "ignore"
    assert QQBridge(tmp_path, store.pool).settings["owner_qq"] == "12345678"
    web = await store.record("webchat", "owner", "web-local", "1", "user", "聊天测试")
    qq = await store.record("aiocqhttp", "12345678", "qq-local", "2", "user", "继续刚才")
    assert web["person_id"] == qq["person_id"] == OWNER
    async with store.pool.connection() as conn:
        phantom = await (
            await conn.execute("SELECT id FROM persons WHERE id='aiocqhttp:12345678'")
        ).fetchone()
    assert phantom is None
    history, _ = await store.continuity.build(OWNER, qq, None)
    assert history and "聊天测试" in history[0]["content"]


async def test_qr_refresh_does_not_reset_scan_in_progress_and_auth_renews(tmp_path):
    s = settings(tmp_path)
    calls = []
    phase = ["waiting_qrcode"]
    online = [False]

    def respond(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/auth/login"):
            assert (
                json.loads(request.content)["hash"]
                == hashlib.sha256((s["webui_token"] + ".napcat").encode()).hexdigest()
            )
            data = {"Credential": "test-credential"}
        else:
            assert request.headers["authorization"] == "Bearer test-credential"
            if path.endswith("/CheckLoginStatus"):
                data = {
                    "isLogin": online[0],
                    "qrcodeurl": "https://example.com/public-qr-challenge",
                    "loginPhase": phase[0],
                }
            elif path.endswith("/GetQQLoginInfo"):
                data = {"uin": "99887766"}
            else:
                data = {}
        return httpx.Response(200, json={"code": 0, "data": data})

    bridge = QQBridge(tmp_path, None, httpx.MockTransport(respond))
    await bridge.poll()
    assert bridge.snapshot()["qr_image"].startswith("data:image/png;base64,")
    bridge.state["qr_image"] = None
    await bridge.poll()
    assert bridge.snapshot()["qr_image"], "A recovered API must restore the still-valid QR"
    phase[0] = "qrcode_scanned"
    bridge.refreshed_at = time.monotonic() - 120
    await bridge.poll()
    assert not any(p.endswith("RefreshQRcode") for p in calls)
    phase[0] = "waiting_qrcode"
    await bridge.poll()
    assert any(p.endswith("RefreshQRcode") for p in calls)
    bridge.auth_at = time.monotonic() - 3100
    online[0] = True
    await bridge.poll()
    assert bridge.snapshot()["online"] and bridge.snapshot()["qr_image"] is None
    assert bridge.snapshot()["bot_qq"] == "99887766"
    assert calls.count("/api/auth/login") == 2
    assert any(p.endswith("SetQuickLoginQQ") for p in calls)


async def test_bridge_close_cancels_poll_and_stops_its_owned_client(tmp_path, monkeypatch):
    settings(tmp_path)
    bridge = QQBridge(tmp_path, None)
    stopped = []
    monkeypatch.setattr(bridge, "stop", lambda: stopped.append(True))
    bridge.task = asyncio.create_task(asyncio.sleep(100))
    await bridge.close()
    assert bridge.task.cancelled() and stopped == [True]


def test_process_scope_excludes_desktop_qq_and_other_node_projects(tmp_path, monkeypatch):
    settings(tmp_path)
    bridge = QQBridge(tmp_path, None)
    own = SimpleNamespace(
        info={"name": "node.exe"},
        exe=lambda: str(bridge.node),
        cmdline=lambda: [str(bridge.node), str(bridge.script)],
    )
    other = SimpleNamespace(
        info={"name": "node.exe"},
        exe=lambda: str(Path("C:/unrelated/node.exe")),
        cmdline=lambda: ["node", "other.js"],
    )
    desktop = SimpleNamespace(info={"name": "QQ.exe"})
    monkeypatch.setattr("plugin.qq.psutil.process_iter", lambda fields: [own, other, desktop])
    assert bridge.owned() == [own]


def test_failed_client_start_is_limited_to_three_attempts(tmp_path, monkeypatch):
    settings(tmp_path)
    bridge = QQBridge(tmp_path, None)
    bridge.spawn_attempts = 3
    monkeypatch.setattr(bridge, "owned", lambda: [])
    with pytest.raises(RuntimeError, match="连续启动失败"):
        bridge.start()
