import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from bot_controller import BotStatus
from gui import STYLE, DesktopApp


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    return app


def until(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "GUI action did not finish"


class FakeController:
    chat_url = "http://127.0.0.1:6185/#/chatbox"
    settings_url = "http://127.0.0.1:6185/#/settings"
    runtime = Path(".")

    def __init__(self, running=True):
        self.running = running
        self.stops = 0
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.release_start.set()

    def status(self):
        return BotStatus(
            "running" if self.running else "stopped",
            "在这里，随时可以聊。",
            pids=(123,) if self.running else (),
            bot_name="小伴",
        )

    def start(self):
        self.start_entered.set()
        assert self.release_start.wait(2)
        self.running = True
        return "正在启动 Bot…"

    def stop(self):
        self.stops += 1
        self.running = False
        return "Bot 已停止。"


def test_busy_idle_display_and_close_to_tray_without_stopping(qt_app):
    controller = FakeController()
    owner = DesktopApp(qt_app, controller, auto_start=False)
    try:
        owner.timer.stop()
        until(qt_app, lambda: "poll" not in owner.jobs)
        assert owner.window.state.text() == "正在运行 · 空闲"
        owner.on_result(
            "poll", BotStatus("busy", "正在忙，稍等一会儿。", pids=(123,), bot_name="小伴"), ""
        )
        assert owner.tray.toolTip() == "陪伴 Bot · 正在运行 · 忙碌"
        owner.tray_available = True
        owner.show_window()
        owner.window.close()
        assert not owner.window.isVisible() and not owner.exiting
        assert controller.stops == 0
        owner.show_window()
        assert owner.window.isVisible()
    finally:
        owner.exit()
        until(qt_app, lambda: not owner.jobs)
        owner.pool.waitForDone(1000)
        owner.window.close()


def test_exit_during_start_waits_then_stops_the_started_backend(qt_app):
    controller = FakeController(running=False)
    controller.release_start.clear()
    owner = DesktopApp(qt_app, controller, auto_start=False)
    try:
        owner.timer.stop()
        until(qt_app, lambda: not owner.jobs)
        owner.toggle()
        assert controller.start_entered.wait(1)
        owner.exit()
        assert "exit" not in owner.jobs
        controller.release_start.set()
        until(qt_app, lambda: not owner.jobs and controller.stops == 1)
        assert not controller.running
    finally:
        controller.release_start.set()
        owner.pool.waitForDone(2000)
        owner.tray.hide()
        owner.exiting = True
        owner.window.close()
