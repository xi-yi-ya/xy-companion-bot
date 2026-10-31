"""Real packaged EXE startup, busy/idle, single-instance and clean-exit checks."""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg

from bot_controller import BotController
from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "runtime/desktop/CompanionBot/CompanionBot.exe"
SNAPSHOT = ROOT / "runtime/qa/desktop-state.json"
sys.stdout.reconfigure(encoding="utf-8")


def wait_for(predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for the desktop state")


def snapshot():
    return json.loads(SNAPSHOT.read_text("utf-8")) if SNAPSHOT.exists() else {}


def send_test_chat(base):
    with httpx.Client(base_url=base, trust_env=False, timeout=120) as client:
        session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
        response = client.post(
            "/api/chat/send",
            json={
                "session_id": session,
                "message": "这是一条托盘忙闲状态技术验收：请读取 https://api-docs.deepseek.com/guides/search/ "
                "并用两句说明搜索与网页读取的区别，附来源链接。不要把本次技术验收写成用户长期事实。",
            },
        )
        response.raise_for_status()
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert any(event.get("type") == "complete" for event in events), "No real chat response"
        return session


def main():
    controller = BotController(ROOT)
    gui = subprocess.Popen([str(EXE), "--diagnostics"], cwd=ROOT)
    initial = wait_for(
        lambda: (
            data
            if (data := snapshot()).get("pid") == gui.pid and data.get("state") == "running"
            else None
        )
    )
    assert (
        initial["platform"] == "windows" and initial["tray_available"] and initial["tray_visible"]
    )
    assert initial["window_visible"]
    print("Packaged EXE native Windows window + registered tray + ready backend: PASS", flush=True)
    before = tuple(p.pid for p in controller.owned_processes())
    duplicate = subprocess.run([str(EXE)], cwd=ROOT, timeout=12, check=False)
    assert (
        duplicate.returncode == 0 and tuple(p.pid for p in controller.owned_processes()) == before
    )
    assert snapshot()["pid"] == gui.pid
    print("Repeated EXE launch reuses one GUI and the existing backend: PASS", flush=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        chat = pool.submit(send_test_chat, controller.base_url)
        busy = wait_for(
            lambda: data if (data := snapshot()).get("state") == "busy" else None, timeout=35
        )
        assert "忙碌" in busy["label"]
        session = chat.result(timeout=120)
    wait_for(lambda: snapshot().get("state") == "running")
    print("Real chat + webpage fetch changes tray busy -> idle: PASS", flush=True)
    shutdown = subprocess.run([str(EXE), "--quit"], cwd=ROOT, timeout=10, check=False)
    assert shutdown.returncode == 0
    gui.wait(timeout=30)
    assert gui.returncode == 0 and not controller.owned_processes()
    assert not controller.port_occupied()
    with psycopg.connect(**database_parameters()) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    print("Quit action stops GUI and backend cleanly; PostgreSQL stays online: PASS", flush=True)
    restarted = subprocess.Popen([str(EXE), "--diagnostics"], cwd=ROOT)
    final = wait_for(
        lambda: (
            data
            if (data := snapshot()).get("pid") == restarted.pid and data.get("state") == "running"
            else None
        ),
        timeout=45,
    )
    (ROOT / "runtime/qa/desktop-live-evidence.json").write_text(
        json.dumps(
            {
                "initial": initial,
                "backend_pids": before,
                "busy": busy,
                "session": session,
                "clean_exit": True,
                "postgresql_unchanged": True,
                "restart": final,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print("Double-click EXE starts the stopped backend and leaves it online: PASS", flush=True)


if __name__ == "__main__":
    main()
