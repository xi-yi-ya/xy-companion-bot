import asyncio
from types import SimpleNamespace

import httpx
import pytest

from bot_controller import BotController
from local_instance import InstanceGuard, instance_id
from local_web import LocalWebMiddleware, keep_shutdown_alive
from plugin.activity import ActivityTracker


def controller(tmp_path):
    (tmp_path / "local_config.json").write_text('{"port":6185}', "utf-8")
    return BotController(tmp_path)


def test_mutex_separates_installations_and_prevents_duplicate_backend(tmp_path):
    first = InstanceGuard(tmp_path)
    duplicate = InstanceGuard(tmp_path)
    other = InstanceGuard(tmp_path / "other")
    try:
        assert first.acquire()
        assert not duplicate.acquire()
        assert other.acquire()
        first.close()
        assert duplicate.acquire()
    finally:
        first.close()
        duplicate.close()
        other.close()


def test_process_ownership_requires_exact_script_and_run_argument(tmp_path, monkeypatch):
    bot = controller(tmp_path)

    def process(pid, args, name="python.exe"):
        return SimpleNamespace(
            pid=pid, info={"pid": pid, "cmdline": args, "name": name, "create_time": 1}
        )

    processes = [
        process(10, ["python.exe", str(tmp_path / "start.py"), "--run"]),
        process(11, ["python.exe", str(tmp_path / "another/start.py"), "--run"]),
        process(12, ["python.exe", "-c", "print(" + str(tmp_path / "start.py") + ")", "--run"]),
        process(13, ["python.exe", str(tmp_path / "start.py"), "--prepare-only"]),
        process(14, ["postgres.exe", str(tmp_path / "start.py"), "--run"], name="postgres.exe"),
    ]
    monkeypatch.setattr("bot_controller.psutil.process_iter", lambda attrs: processes)
    assert [p.pid for p in bot.owned_processes()] == [10]


def test_stop_prefers_native_cleanup_and_does_not_terminate_unrelated_process(
    tmp_path, monkeypatch
):
    bot = controller(tmp_path)
    target = SimpleNamespace(pid=10)
    monkeypatch.setattr(bot, "owned_processes", lambda: [target])
    paths = []

    def request(path, method="GET"):
        paths.append((path, method))
        return {"instance_id": bot.identity, "pid": 10, "accepted": True}

    monkeypatch.setattr(bot, "request", request)
    monkeypatch.setattr(
        "bot_controller.psutil.wait_procs", lambda processes, timeout: (processes, [])
    )
    assert "已停止" in bot.stop()
    assert ("/companion/shutdown", "POST") in paths


async def test_activity_handles_overlapping_jobs_failures_and_cancellation():
    tracker = ActivityTracker()
    hold = asyncio.Event()

    async def work():
        tracker.begin("正在回复", asyncio.current_task())
        await hold.wait()

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    with tracker.working("正在读网页"):
        assert tracker.snapshot()["busy"]
    assert tracker.snapshot()["busy"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tracker.snapshot()["busy"]
    with pytest.raises(ValueError), tracker.working("整理记忆"):
        raise ValueError("simulated failure")
    assert not tracker.snapshot()["busy"]


async def test_shutdown_requires_post_matching_installation_and_local_origin(tmp_path):
    stopped = asyncio.Event()

    async def stop():
        stopped.set()

    core = SimpleNamespace(stop=stop)
    auth = SimpleNamespace(generate_jwt=lambda *args, **kwargs: "test-local-owner")
    server = SimpleNamespace(
        core_lifecycle=core,
        config={"dashboard": {"username": "owner"}},
        asgi_app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(auth=auth))),
    )

    async def inner(scope, receive, send):
        raise AssertionError("No passthrough expected")

    app = LocalWebMiddleware(inner, server, 6185)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:6185"
    ) as client:
        assert (await client.get("/companion/shutdown")).status_code == 405
        assert (await client.post("/companion/shutdown")).status_code == 403
        headers = {"X-Companion-Instance": app.instance_id, "Origin": "https://unrelated.example"}
        assert (await client.post("/companion/shutdown", headers=headers)).status_code == 403
        headers.pop("Origin")
        assert (await client.post("/companion/shutdown", headers=headers)).json()["accepted"]
        await asyncio.wait_for(stopped.wait(), 1)
        await core._companion_shutdown_task


async def test_lifecycle_does_not_finish_before_manager_cleanup():
    class FakeLifecycle:
        def __init__(self):
            self.main_done = asyncio.Event()
            self.cleaned = False

        async def start(self):
            await self.main_done.wait()

        async def stop(self):
            self.main_done.set()
            await asyncio.sleep(0.01)
            self.cleaned = True

    keep_shutdown_alive(FakeLifecycle)
    core = FakeLifecycle()
    running = asyncio.create_task(core.start())
    core._companion_shutdown_task = asyncio.create_task(core.stop())
    await running
    assert core.cleaned
    assert instance_id(".") == instance_id("./")
