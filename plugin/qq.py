"""Owned NapCat lifecycle, QR-login status, and explicit cross-channel pairing."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import httpx
import psutil
import qrcode

PLATFORM_ID = "companion_qq"
OWNER = "webchat:owner"


def guard_waking_stage(stage_class, bridge, ingress=None):
    """Pair and gate before native command/permission handling can reply."""
    stage_class._companion_qq_bridge = bridge
    stage_class._companion_qq_ingress = ingress
    if getattr(stage_class, "_companion_qq_guard", False):
        return
    original = stage_class.process

    async def process(stage, event):
        if event.get_platform_name() == "aiocqhttp":
            active_ingress = stage_class._companion_qq_ingress
            if not active_ingress and not event.is_private_chat():
                event.stop_event()
                return
            try:
                result = (
                    await active_ingress.authorize(event)
                    if active_ingress
                    else await stage_class._companion_qq_bridge.authorize(
                        str(event.get_sender_id()), event.message_str
                    )
                )
            except Exception:
                event.stop_event()
                return
            if result != "allow":
                event.stop_event()
                if result == "paired":
                    await event.send(
                        event.plain_result(
                            "绑定好了。这里和本地 Web 会共用我们的记忆与习惯，可以直接聊。"
                        )
                    )
                return
            if active_ingress:
                try:
                    if not await active_ingress.process(event):
                        return
                except Exception:
                    event.stop_event()
                    await event.send(event.plain_result("本地消息记录暂时不可用，请稍后重试。"))
                    return
        return await original(stage, event)

    stage_class.process = process
    stage_class._companion_qq_guard = True


def configure_platform(config, folder):
    path = Path(folder) / "settings.json"
    if not path.exists():
        return
    settings = json.loads(path.read_text("utf-8"))
    platforms = config.setdefault("platform", [])
    target = next((p for p in platforms if p.get("id") == PLATFORM_ID), None)
    if target is None:
        target = {"id": PLATFORM_ID, "type": "aiocqhttp"}
        platforms.append(target)
    target.update(
        enable=settings["enabled"],
        ws_reverse_host="127.0.0.1",
        ws_reverse_port=settings["ws_port"],
        ws_reverse_token=settings["ws_token"],
    )


class QQBridge:
    def __init__(self, folder, pool, transport=None):
        self.folder, self.pool = Path(folder).resolve(), pool
        path = self.folder / "settings.json"
        self.settings = json.loads(path.read_text("utf-8")) if path.exists() else {}
        self.transport = transport
        self.node = self.folder / "napcat/node.exe"
        self.script = self.folder / "napcat/index.js"
        self.task = None
        self.credential = None
        self.auth_at = 0
        self.refreshed_at = 0
        self.last_qr = None
        self.spawn_attempts = 0
        self.api_down_since = None
        self.restart_not_before = 0
        self.lock = asyncio.Lock()
        self.state = {"state": "disabled", "online": False, "qr_image": None}

    def owned(self):
        result = []
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info["name"] or "").lower() != "node.exe":
                continue
            try:
                if Path(p.exe()).resolve() != self.node:
                    continue
                if any(
                    Path(a).resolve() == self.script
                    for a in p.cmdline()[1:]
                    if not a.startswith("-")
                ):
                    result.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
        return result

    def start(self):
        if self.owned():
            return
        if self.spawn_attempts >= 3:
            raise RuntimeError("QQ 客户端连续启动失败，重启 Bot 后可重新尝试；日志在 runtime/qq。")
        if not self.node.is_file() or not self.script.is_file():
            raise ValueError("QQ 组件尚未安装，请运行 install_qq.py")
        env = dict(os.environ, NAPCAT_QQ_DATA_DIR=str(self.folder / "data"))
        # Avoid unrelated DLLs (including PostgreSQL ICU) in the native wrapper.
        system = Path(os.environ.get("SystemRoot", "C:/Windows"))
        env["PATH"] = os.pathsep.join(map(str, [self.node.parent, system / "System32", system]))
        self.spawn_attempts += 1
        with (
            (self.folder / "stdout.log").open("ab") as out,
            (self.folder / "stderr.log").open("ab") as err,
        ):
            subprocess.Popen(
                [str(self.node), str(self.script)],
                cwd=self.node.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def stop(self):
        for process in self.owned():
            try:
                children = process.children(recursive=True)
                process.terminate()
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs([process, *children], timeout=5)
            except psutil.NoSuchProcess:
                pass

    async def call(self, path, data=None, authenticate=True):
        base = f"http://127.0.0.1:{self.settings['webui_port']}/api"
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, transport=self.transport
        ) as client:
            if authenticate and (not self.credential or time.monotonic() - self.auth_at > 3000):
                hashed = hashlib.sha256(
                    (self.settings["webui_token"] + ".napcat").encode()
                ).hexdigest()
                reply = (await client.post(base + "/auth/login", json={"hash": hashed})).json()
                self.credential = reply.get("data", {}).get("Credential")
                if not self.credential:
                    raise ValueError("QQ 本地管理服务认证失败")
                self.auth_at = time.monotonic()
            headers = {"Authorization": "Bearer " + self.credential} if authenticate else {}
            response = await client.post(base + path, json=data or {}, headers=headers)
            response.raise_for_status()
            reply = response.json()
            if reply.get("code") != 0:
                self.credential = None
                raise ValueError(str(reply.get("message") or "QQ 本地接口暂不可用"))
            return reply.get("data") or {}

    @staticmethod
    def image(value):
        output = io.BytesIO()
        qrcode.make(value).save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()

    async def refresh(self):
        async with self.lock:
            if self.state.get("online"):
                return
            await self.call("/QQLogin/RefreshQRcode")
            self.refreshed_at = time.monotonic()

    async def poll(self):
        async with self.lock:
            login = await self.call("/QQLogin/CheckLoginStatus")
            online = bool(login.get("isLogin"))
            phase = login.get("loginPhase", "")
            self.state.update(
                state="online" if online else "waiting_scan",
                online=online,
                phase=phase,
                error=login.get("loginError"),
            )
            if online:
                self.state["qr_image"] = None
                info = await self.call("/QQLogin/GetQQLoginInfo")
                self.state["bot_qq"] = str(info.get("uin") or "")
                if self.state["bot_qq"] and not self.state.get("auto_login_set"):
                    await self.call("/QQLogin/SetQuickLoginQQ", {"uin": self.state["bot_qq"]})
                    self.state["auto_login_set"] = True
                return
            value = login.get("qrcodeurl")
            if value and (value != self.last_qr or not self.state.get("qr_image")):
                self.state["qr_image"] = await asyncio.to_thread(self.image, value)
                self.last_qr = value
                self.refreshed_at = time.monotonic()
            if (
                phase not in ("qrcode_scanned", "initializing", "reconnecting")
                and time.monotonic() - self.refreshed_at > 90
            ):
                await self.call("/QQLogin/RefreshQRcode")
                self.refreshed_at = time.monotonic()

    async def run(self):
        if not self.settings.get("enabled"):
            return
        while True:
            try:
                if time.monotonic() < self.restart_not_before:
                    await asyncio.sleep(5)
                    continue
                await asyncio.to_thread(self.start)
                await self.poll()
                self.api_down_since = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.update(
                    state="starting" if self.spawn_attempts < 3 else "error",
                    online=False,
                    error=type(exc).__name__ + ": " + str(exc),
                    qr_image=None,
                )
                # Windows can briefly reject a rapid rebind after shutdown.
                # A live native process without WebUI must not stay stuck all night.
                now = time.monotonic()
                if self.api_down_since is None:
                    self.api_down_since = now
                elif now - self.api_down_since >= 20 and self.spawn_attempts < 3:
                    await asyncio.to_thread(self.stop)
                    self.credential = None
                    self.api_down_since = None
                    self.restart_not_before = time.monotonic() + 10
            await asyncio.sleep(5)

    async def open(self):
        if self.settings.get("enabled"):
            self.state["state"] = "starting"
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.to_thread(self.stop)

    def snapshot(self):
        return {
            **self.state,
            "enabled": bool(self.settings.get("enabled")),
            "owner_qq": self.settings.get("owner_qq", ""),
            "bind_command": "/绑定 " + self.settings["bind_code"]
            if self.settings and not self.settings.get("owner_qq")
            else None,
        }

    async def authorize(self, account, content):
        """No raw messages or LLM calls before explicit pairing with local UI code."""
        if not self.settings.get("enabled"):
            return "ignore"
        if self.settings.get("owner_qq"):
            return "allow" if account == self.settings["owner_qq"] else "ignore"
        expected = "/绑定 " + self.settings["bind_code"]
        # The framework's wake-prefix stage may remove the leading slash.
        entered = content.strip()
        if not entered.startswith("/"):
            entered = "/" + entered
        if not secrets.compare_digest(entered.encode(), expected.encode()) or not re.fullmatch(
            r"[1-9][0-9]{4,11}", account
        ):
            return "ignore"
        async with self.lock:
            if self.settings.get("owner_qq"):
                return "ignore"
            async with self.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO persons(id) VALUES(%s) ON CONFLICT DO NOTHING", (OWNER,)
                )
                await conn.execute(
                    "INSERT INTO platform_accounts(platform,account_id,person_id) VALUES('aiocqhttp',%s,%s) ON CONFLICT(platform,account_id) DO UPDATE SET person_id=EXCLUDED.person_id",
                    (account, OWNER),
                )
            updated = {
                **self.settings,
                "owner_qq": account,
                "bind_code": secrets.token_hex(6).upper(),
            }
            path = self.folder / "settings.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, indent=2), "utf-8")
            temp.replace(path)
            self.settings = updated
        return "paired"
