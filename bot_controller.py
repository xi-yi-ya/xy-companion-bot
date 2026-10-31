"""Desktop process control; only the exact backend belonging to this checkout."""

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import psutil

from local_instance import instance_id

API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/status"


@dataclass(frozen=True)
class BotStatus:
    state: str
    detail: str
    pids: tuple[int, ...] = ()
    database: bool = False
    model: str = ""
    bot_name: str = "陪伴 Bot"
    uptime: int = 0


class BotController:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runtime = self.root / "runtime"
        self.identity = instance_id(self.root)
        self.lock = threading.RLock()
        self.last_error = ""
        self.children = []
        self.was_running = False
        config = json.loads((self.root / "local_config.json").read_text("utf-8"))
        self.port = int(config.get("port", 6185))
        if not 1 <= self.port <= 65535:
            raise ValueError("本机服务端口无效")
        self.base_url = f"http://127.0.0.1:{self.port}"

    @property
    def chat_url(self):
        return self.base_url + "/#/chatbox"

    @property
    def settings_url(self):
        return self.base_url + "/#/plugin-view/astrbot_plugin_companion_memory/memories"

    def owned_processes(self):
        script = os.path.normcase(str(self.root / "start.py"))
        result = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                info = process.info
                # An exact script argument prevents matching another checkout,
                # an editor, this GUI, or a process that only mentions the path.
                if (info["name"] or "").lower() not in (
                    "python.exe",
                    "pythonw.exe",
                ):
                    continue
                args = info.get("cmdline") or process.cmdline() or []
                if "--run" not in args:
                    continue
                if any(
                    os.path.normcase(str(Path(arg).absolute())) == script
                    for arg in args[1:]
                    if not arg.startswith("-")
                ):
                    info["create_time"] = info.get("create_time") or process.create_time()
                    result.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return result

    def request(self, path, method="GET"):
        request = Request(
            self.base_url + path,
            data=b"{}" if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Companion-Instance": self.identity},
        )
        # Never send localhost requests through inherited proxies.
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            return json.loads(response.read(64000))

    def port_occupied(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                return True
        except OSError:
            return False

    def status(self):
        processes = self.owned_processes()
        pids = tuple(process.pid for process in processes)
        try:
            data = self.request(API)
        except (OSError, ValueError, HTTPError, URLError):
            data = {}
        matching = (
            data.get("application") == "xy-companion-bot"
            and data.get("instance_id") == self.identity
            and data.get("pid") in pids
        )
        if processes:
            age = max(0, int(time.time() - min(p.info["create_time"] for p in processes)))
            if matching and data.get("ready"):
                self.was_running = True
                return BotStatus(
                    "busy" if data.get("busy") else "running",
                    "正在忙，稍等一会儿。" if data.get("busy") else "在这里，随时可以聊。",
                    pids,
                    True,
                    data.get("model", ""),
                    data.get("bot_name", "陪伴 Bot"),
                    age,
                )
            if matching and not data.get("database"):
                return BotStatus(
                    "error", "记忆数据库暂时不可用，请检查 PostgreSQL 服务", pids, False, uptime=age
                )
            if age < 90:
                return BotStatus("starting", "正在载入模型、插件与记忆数据库…", pids, uptime=age)
            return BotStatus(
                "error", "后端进程存在，但服务尚未就绪；可查看运行日志", pids, uptime=age
            )
        if self.port_occupied():
            return BotStatus("error", f"端口 {self.port} 已被其他服务占用，请先检查占用情况")
        exited = [p for p in self.children if p.poll() is not None]
        if exited or self.was_running:
            code = exited[-1].returncode if exited else None
            return BotStatus(
                "error",
                "Bot 意外停止"
                + (f"（退出码 {code}）" if code is not None else "")
                + "，可以重新启动或查看日志。",
            )
        return BotStatus("stopped", self.last_error or "启动后即可使用本机聊天；关闭窗口可留在托盘")

    def start(self):
        with self.lock:
            self.children = [p for p in self.children if p.poll() is None]
            existing = self.owned_processes()
            if existing:
                return "Bot 已在运行，已连接现有实例。"
            if self.port_occupied():
                raise RuntimeError(f"端口 {self.port} 已被占用，未启动重复实例。")
            python = self.root / ".venv/Scripts/python.exe"
            if not python.is_file() or not (self.root / "start.py").is_file():
                raise RuntimeError("未找到本地运行环境，请检查项目目录。")
            self.runtime.mkdir(exist_ok=True)
            with (
                (self.runtime / "stdout.log").open("ab") as out,
                (self.runtime / "stderr.log").open("ab") as err,
            ):
                child = subprocess.Popen(
                    [str(python), str(self.root / "start.py"), "--run"],
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            self.children.append(child)
            self.last_error = ""
            self.was_running = False
            return "正在启动 Bot…"

    def stop(self):
        with self.lock:
            processes = self.owned_processes()
            if not processes:
                self.was_running = False
                self.children = []
                return "Bot 已停止。"
            graceful = False
            try:
                health = self.request(API)
                if health.get("instance_id") == self.identity and health.get("pid") in {
                    p.pid for p in processes
                }:
                    graceful = bool(self.request("/companion/shutdown", "POST").get("accepted"))
            except (OSError, ValueError, HTTPError, URLError):
                pass
            # Give the native lifecycle time to flush and close its managers.
            _, alive = psutil.wait_procs(processes, timeout=15 if graceful else 0)
            forced = bool(alive)
            for process in alive:
                # Re-check command lines to protect against stale PID reuse.
                if process.pid in {p.pid for p in self.owned_processes()}:
                    try:
                        process.terminate()
                    except psutil.NoSuchProcess:
                        pass
            _, alive = psutil.wait_procs(alive, timeout=5)
            if alive:
                raise RuntimeError("Bot 进程未能停止，请查看运行日志。")
            self.children = [p for p in self.children if p.poll() is None]
            self.last_error = ""
            self.was_running = False
            return "Bot 已停止。" + ("（后端未及时响应，已结束本项目进程。）" if forced else "")
