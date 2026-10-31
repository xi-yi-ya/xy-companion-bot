"""A quiet Windows tray companion, with one small optional control window."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from bot_controller import BotController, BotStatus
from desktop_art import COLORS, bot_icon
from local_instance import instance_id

ROOT = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
if getattr(sys, "frozen", False):
    ROOT = Path((ROOT / "companion-root.txt").read_text("utf-8").strip())

LABELS = {
    "running": "正在运行 · 空闲",
    "busy": "正在运行 · 忙碌",
    "starting": "正在启动",
    "stopped": "已停止",
    "error": "需要看一下",
}
STYLE = """
QWidget {font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #2e4636;}
QMainWindow, QWidget#surface {background: #f5f6f0;}
QLabel#eyebrow {font-size: 10px; letter-spacing: 2px; color: #7b8c7d;}
QLabel#title {font-size: 25px; font-weight: 600;}
QLabel#subtitle, QLabel#footer {color: #7b897c; font-size: 12px;}
QFrame#card {background: #fffef9; border: 1px solid #dfe6da; border-radius: 18px;}
QLabel#state {font-size: 24px; font-weight: 600;}
QLabel#detail {color: #718071; font-size: 13px;}
QPushButton {background: #fffef9; border: 1px solid #d7e0d3; border-radius: 9px; padding: 11px 18px;}
QPushButton:hover {background: #eaf0e6;}
QPushButton:disabled {color: #a0aaa0; background: #f0f2eb;}
QPushButton#chat {background: #42674e; color: #fff; border-color: #42674e;}
QPushButton#chat:hover {background: #34563f;}
QPushButton#logs {border: 0; color: #7b897c; padding: 6px 0; font-size: 11px;}
QMenu {background: #fffef9; border: 1px solid #dce3d7; padding: 7px;}
QMenu::item {padding: 8px 22px; border-radius: 5px;}
QMenu::item:selected {background: #e6eee1;}
QMenu::item:disabled {color: #8c988d;}
QMenu::separator {height: 1px; background: #e5e9de; margin: 5px 8px;}
"""


class WorkerSignals(QObject):
    completed = Signal(str, object, str)


class Worker(QRunnable):
    def __init__(self, key, function):
        super().__init__()
        self.key, self.function = key, function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.completed.emit(self.key, self.function(), "")
        except Exception as exc:
            logging.exception("Desktop operation failed: %s", self.key)
            self.signals.completed.emit(self.key, None, str(exc))


class CompanionWindow(QMainWindow):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setWindowTitle("陪伴 Bot")
        self.setWindowIcon(bot_icon())
        self.setMinimumSize(480, 420)
        self.resize(510, 460)
        surface = QWidget()
        surface.setObjectName("surface")
        self.setCentralWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(32, 28, 32, 22)
        layout.setSpacing(10)
        eyebrow = QLabel("YOUR LOCAL COMPANION")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)
        self.title = QLabel("陪伴 Bot")
        self.title.setObjectName("title")
        layout.addWidget(self.title)
        subtitle = QLabel("留在托盘里，想聊的时候就打开。")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(14)
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(24, 24, 24, 22)
        inner.setSpacing(10)
        self.state = QLabel("正在看看状态…")
        self.state.setObjectName("state")
        inner.addWidget(self.state)
        self.detail = QLabel("")
        self.detail.setObjectName("detail")
        self.detail.setWordWrap(True)
        inner.addWidget(self.detail)
        inner.addSpacing(8)
        links = QHBoxLayout()
        self.chat = QPushButton("打开聊天")
        self.chat.setObjectName("chat")
        self.chat.clicked.connect(owner.open_chat)
        settings = QPushButton("Bot 设置")
        settings.setObjectName("settings")
        settings.clicked.connect(owner.open_settings)
        links.addWidget(self.chat)
        links.addWidget(settings)
        inner.addLayout(links)
        layout.addWidget(card)
        self.message = QLabel("")
        self.message.setObjectName("detail")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        self.toggle = QPushButton("启动 Bot")
        self.toggle.setObjectName("toggle")
        self.toggle.clicked.connect(owner.toggle)
        hide = QPushButton("收进托盘")
        hide.setObjectName("hide")
        hide.clicked.connect(owner.hide_window)
        exit_button = QPushButton("退出")
        exit_button.setObjectName("exit")
        exit_button.clicked.connect(owner.exit)
        buttons.addWidget(self.toggle)
        buttons.addStretch(1)
        buttons.addWidget(hide)
        buttons.addWidget(exit_button)
        layout.addLayout(buttons)
        footer = QHBoxLayout()
        label = QLabel("退出会停止 Bot，PostgreSQL 继续运行。")
        label.setObjectName("footer")
        footer.addWidget(label)
        logs = QPushButton("查看日志")
        logs.setObjectName("logs")
        logs.clicked.connect(owner.open_logs)
        footer.addWidget(logs)
        layout.addLayout(footer)

    def closeEvent(self, event):
        if self.owner.exiting or not self.owner.tray_available:
            event.accept()
            if not self.owner.exiting:
                self.owner.exit()
        else:
            event.ignore()
            self.owner.hide_window()


class DesktopApp(QObject):
    def __init__(self, app, controller, auto_start=True):
        super().__init__()
        self.app, self.controller = app, controller
        self.status = BotStatus("stopped", "正在读取状态…")
        self.initial_start = auto_start
        self.exiting = False
        self.jobs = {}
        self.diagnostics = False
        self.last_snapshot = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.window = CompanionWindow(self)
        self.tray = QSystemTrayIcon(bot_icon("stopped"), self)
        self.menu = QMenu()
        self.state_action = self.menu.addAction("正在读取状态…")
        self.state_action.setEnabled(False)
        self.menu.addSeparator()
        self.chat_action = self.menu.addAction("打开聊天", self.open_chat)
        self.menu.addAction("Bot 设置", self.open_settings)
        self.menu.addAction("打开控制窗口", self.show_window)
        self.menu.addSeparator()
        self.toggle_action = self.menu.addAction("启动 Bot", self.toggle)
        self.menu.addAction("退出（停止 Bot）", self.exit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.activate_tray)
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        self.poll()

    def show_window(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.write_snapshot()

    def hide_window(self):
        if self.tray_available:
            self.window.hide()
        else:
            self.window.showMinimized()
        self.write_snapshot()

    def activate_tray(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def open_chat(self):
        QDesktopServices.openUrl(QUrl(self.controller.chat_url))

    def open_settings(self):
        QDesktopServices.openUrl(QUrl(self.controller.settings_url))

    def open_logs(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.runtime)))

    def job(self, key, function):
        if key in self.jobs:
            return
        worker = Worker(key, function)
        self.jobs[key] = worker
        worker.signals.completed.connect(self.on_result)
        self.pool.start(worker)

    def poll(self):
        if not self.exiting:
            self.job("poll", self.controller.status)

    @Slot(str, object, str)
    def on_result(self, key, result, error):
        self.jobs.pop(key, None)
        if self.exiting and key in ("start", "stop"):
            self.job("exit", self.controller.stop)
            self.render()
            return
        if self.exiting and key != "exit":
            return
        if error:
            self.window.message.setText("操作未完成：" + error)
            if key == "exit":
                self.exiting = False
                self.timer.start(2000)
                self.show_window()
            self.render()
            return
        if key == "poll":
            self.status = result
            self.render()
            if self.initial_start:
                self.initial_start = False
                if result.state == "stopped":
                    self.job("start", self.controller.start)
                    self.render()
        elif key == "exit":
            self.tray.hide()
            self.app.quit()
        else:
            self.window.message.setText(result)
            self.render()
            self.poll()

    def render(self):
        state = self.status.state
        text = LABELS.get(state, "正在读取状态…")
        active = bool(self.status.pids)
        pending = any(key in self.jobs for key in ("start", "stop", "exit"))
        if pending:
            text = (
                "正在退出…" if self.exiting else "正在停止…" if "stop" in self.jobs else "正在启动…"
            )
        self.window.state.setText(text)
        self.window.state.setStyleSheet("color:" + COLORS.get(state, COLORS["stopped"]))
        self.window.detail.setText(self.status.detail)
        self.window.title.setText(self.status.bot_name or "陪伴 Bot")
        self.window.toggle.setText("停止 Bot" if active else "启动 Bot")
        self.toggle_action.setText("停止 Bot" if active else "启动 Bot")
        self.window.toggle.setEnabled(not pending and state != "starting")
        self.toggle_action.setEnabled(not pending and state != "starting")
        ready = state in ("running", "busy")
        self.window.chat.setEnabled(ready)
        self.chat_action.setEnabled(ready)
        self.tray.setIcon(bot_icon(state))
        self.window.setWindowIcon(bot_icon(state))
        self.tray.setToolTip("陪伴 Bot · " + text)
        self.state_action.setText(text)
        self.write_snapshot()

    def write_snapshot(self):
        if not self.diagnostics:
            return
        data = {
            "pid": os.getpid(),
            "state": self.status.state,
            "label": self.window.state.text(),
            "tray_available": self.tray_available,
            "tray_visible": self.tray.isVisible(),
            "window_visible": self.window.isVisible(),
            "platform": QApplication.platformName(),
            "menu": [action.text() for action in self.menu.actions() if not action.isSeparator()],
        }
        if data == self.last_snapshot:
            return
        self.last_snapshot = data
        target = self.controller.runtime / "qa"
        target.mkdir(parents=True, exist_ok=True)
        (target / "desktop-state.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
        )
        if self.window.isVisible():
            self.window.grab().save(str(target / f"desktop-{self.status.state}.png"))

    def toggle(self):
        if self.exiting or "start" in self.jobs or "stop" in self.jobs:
            return
        self.initial_start = False
        self.job(
            "stop" if self.status.pids else "start",
            self.controller.stop if self.status.pids else self.controller.start,
        )
        self.render()

    def exit(self):
        if self.exiting:
            return
        self.exiting = True
        self.initial_start = False
        self.timer.stop()
        if not any(key in self.jobs for key in ("start", "stop")):
            self.job("exit", self.controller.stop)
        self.render()


def single_instance(app, root, owner_factory, quit_requested=False):
    name = "XYCompanion.gui." + instance_id(root)
    socket = QLocalSocket()
    socket.connectToServer(name)
    if socket.waitForConnected(500):
        socket.write(b"quit\n" if quit_requested else b"show\n")
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return None, None
    if quit_requested:
        return None, None
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(name)
    if not server.listen(name):
        raise RuntimeError("无法创建本地托盘控制通道")
    owner = owner_factory()

    def activate():
        while server.hasPendingConnections():
            client = server.nextPendingConnection()

            def command(client=client):
                if not client.canReadLine():
                    return
                value = bytes(client.readLine()).strip()
                if value == b"quit":
                    owner.exit()
                else:
                    owner.show_window()
                client.disconnectFromServer()
                client.deleteLater()

            client.readyRead.connect(command)
            command()

    server.newConnection.connect(activate)
    return server, owner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--quit", action="store_true")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("陪伴 Bot")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    (ROOT / "runtime").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "runtime/desktop.log", encoding="utf-8", level=logging.INFO)
    try:
        server, owner = single_instance(
            app,
            ROOT,
            lambda: DesktopApp(app, BotController(ROOT), not args.no_auto_start),
            args.quit,
        )
        if owner is None:
            return 0
        owner.diagnostics = args.diagnostics
        if not args.tray or not owner.tray_available:
            owner.show_window()
        code = app.exec()
        owner.pool.waitForDone(3000)
        server.close()
        return code
    except Exception:
        logging.exception("Desktop startup failed")
        QMessageBox.critical(None, "陪伴 Bot", "启动控制窗口失败，请查看项目 runtime/desktop.log。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
