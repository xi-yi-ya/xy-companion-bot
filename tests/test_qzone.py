import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from plugin.proactive import OWNER
from plugin.qzone import DEFAULT_QZONE, QzoneService, review_items, validate_qzone
from plugin.qzone_http import (
    ACTIVE,
    INDEX,
    QzoneReader,
    QzoneReadError,
    normalize_posts,
    parse_cookies,
)
from tests.test_proactive import Harness as ContactHarness
from tests.test_proactive import Midpoint, setup

NOW = datetime(2026, 11, 1, 4, tzinfo=UTC)


def post(key="qzone-test", account="456"):
    return {
        "key": key,
        "account": account,
        "author": "777777",
        "nickname": "测试作者",
        "fid": key,
        "appid": 311,
        "posted_at": int(NOW.timestamp()),
        "body": "这是一条合成测试动态，不是用户事实",
        "images": [],
        "comments": [],
        "url": "https://user.qzone.qq.com/777777/mood/" + key,
    }


def notes(posts, interesting=True):
    return {
        "items": [
            {
                "key": p["key"],
                "interesting": interesting,
                "score": 0.8,
                "note": "作者分享了一个小发现，这是外部观察。",
                "reason": "适合承接共同话题",
            }
            for p in posts
        ]
    }


class Harness:
    def __init__(self, store):
        self.now, self.calls, self.reviews = NOW, 0, 0
        self.posts, self.online, self.busy = [post()], True, False
        self.review_hook = None
        store.qzone.rng = Midpoint()
        self.service = QzoneService(
            store.qzone,
            self.fetch,
            self.review,
            self.available,
            lambda: self.busy,
            lambda: self.now,
        )

    async def available(self):
        return "456" if self.online else None

    async def fetch(self):
        self.calls += 1
        return "456", self.posts

    async def review(self, posts):
        self.reviews += 1
        if self.review_hook:
            await self.review_hook()
        return notes(posts)


async def test_random_schedule_persists_and_does_not_run_before_due(store):
    h = Harness(store)
    assert (await h.service.step())["status"] == "waiting"
    first = (await store.qzone.snapshot())["state"]["due_at"]
    assert first == NOW + timedelta(minutes=22.5)
    h.now += timedelta(minutes=15)
    assert (await h.service.step())["status"] == "waiting" and h.calls == 0
    h.now = first
    assert (await h.service.step())["status"] == "complete" and h.calls == 1
    assert (await store.qzone.snapshot())["state"]["due_at"] == first + timedelta(minutes=22.5)


async def test_repeated_feed_is_read_by_model_once_and_never_becomes_user_memory(store):
    h = Harness(store)
    await h.service.step(force=True)
    await h.service.step(force=True)
    assert h.calls == 2 and h.reviews == 1
    snapshot = await store.qzone.snapshot()
    assert len(snapshot["candidates"]) == 1
    async with store.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM messages")).fetchone())[
            "n"
        ] == 0
        assert (await (await conn.execute("SELECT count(*) AS n FROM memories")).fetchone())[
            "n"
        ] == 0


async def test_empty_feed_offline_and_busy_do_not_call_model(store):
    h = Harness(store)
    h.online = False
    assert (await h.service.step(force=True))["status"] == "offline"
    h.online, h.busy = True, True
    assert (await h.service.step())["status"] == "busy"
    h.busy, h.posts = False, []
    assert (await h.service.step(force=True))["status"] == "complete"
    assert h.calls == 1 and h.reviews == 0


async def test_one_claim_per_lease_and_interrupted_runs_are_recovered(store):
    await store.qzone.claim(NOW, "456", force=True)
    assert await store.qzone.claim(NOW + timedelta(minutes=1), "456", force=True) is None
    assert await store.qzone.claim(NOW + timedelta(minutes=6), "456", force=True)
    runs = (await store.qzone.snapshot())["runs"]
    assert sorted(r["status"] for r in runs) == ["error", "reading"]


async def test_disable_during_read_prevents_candidates_and_future_scans(store):
    h = Harness(store)

    async def disable():
        await store.qzone.save({**DEFAULT_QZONE, "enabled": False}, NOW)

    h.review_hook = disable
    assert (await h.service.step(force=True))["status"] == "cancelled"
    assert not (await store.qzone.snapshot())["candidates"]
    assert (await h.service.step(force=True))["status"] == "waiting"
    assert h.calls == 1


async def test_read_error_is_visible_and_rescheduled_without_hot_retries(store):
    h = Harness(store)

    async def fail():
        raise QzoneReadError("请更新空间登录")

    h.service.fetch = fail
    assert (await h.service.step(force=True))["status"] == "error"
    snapshot = await store.qzone.snapshot()
    assert snapshot["state"]["last_error"] == "请更新空间登录"
    assert snapshot["state"]["due_at"] == NOW + timedelta(minutes=22.5)


async def test_reload_closes_manual_browse_before_database_pool_closes(store):
    h = Harness(store)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    h.service.fetch = blocked
    task = asyncio.create_task(h.service.step(force=True))
    await entered.wait()
    await h.service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = (await store.qzone.snapshot())["state"]
    assert state["current_run"] is None and state["last_error"] == "浏览被中断"


@pytest.mark.parametrize(
    "changes",
    [
        {"min_minutes": 0},
        {"max_minutes": 1441},
        {"enabled": "yes"},
        {"min_minutes": 31, "max_minutes": 30},
        {"min_minutes": True},
    ],
)
def test_invalid_intervals_and_switches_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_qzone({**DEFAULT_QZONE, **changes})


@pytest.mark.parametrize(
    "result",
    [
        {"items": []},
        {"items": [{"key": "forged", "interesting": True, "score": 1, "note": "n", "reason": "r"}]},
        {
            "items": [
                {
                    "key": "qzone-test",
                    "interesting": True,
                    "score": float("nan"),
                    "note": "n",
                    "reason": "r",
                }
            ]
        },
    ],
)
def test_review_cannot_invent_sources_or_scores(result):
    with pytest.raises(ValueError):
        review_items(result, [post()])


async def prepare_share(store):
    await setup(store)
    h = Harness(store)
    await h.service.step(force=True)
    candidate = await store.qzone.pending("456", NOW)
    contact = ContactHarness(store)

    async def generate(run):
        return {
            "send": True,
            "text": "刚看到一条挺有意思的动态。\n" + candidate["payload"]["url"],
            "reason": "分享读到的内容",
            "qzone_candidate_id": candidate["id"],
            "qzone_revision": candidate["revision"],
        }

    contact.service.generate = generate
    return contact, candidate


async def test_share_uses_existing_contact_gate_and_marks_candidate_once(store):
    h, candidate = await prepare_share(store)
    await h.service.step()
    await h.service.step()
    assert len(h.sends) == 1
    state = (await store.contact.snapshot())["state"]
    assert state["awaiting_reply"]
    assert (await store.qzone.snapshot())["candidates"][0]["status"] == "shared"
    assert await store.qzone.pending("456", NOW) is None
    async with store.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT content FROM messages WHERE person_id=%s AND role='assistant'", (OWNER,)
            )
        ).fetchall()
    assert candidate["payload"]["url"] in rows[0]["content"]


async def test_share_disabled_after_generation_cannot_send(store):
    h, _ = await prepare_share(store)
    original = h.service.generate

    async def generate(run):
        result = await original(run)
        await store.qzone.save({**DEFAULT_QZONE, "share_enabled": False}, NOW)
        return result

    h.service.generate = generate
    await h.service.step()
    assert h.sends == []
    assert (await store.qzone.snapshot())["candidates"][0]["status"] == "pending"


async def test_unknown_delivery_never_repeats_the_same_qzone_share(store):
    h, _ = await prepare_share(store)

    async def fail(target, text):
        raise TimeoutError("ack lost")

    h.service.send = fail
    await h.service.step()
    assert (await store.qzone.snapshot())["candidates"][0]["status"] == "unknown"
    assert (await store.contact.snapshot())["state"]["awaiting_reply"]


async def test_http_reader_is_get_only_and_handles_desktop_redirect_without_leaking_cookie():
    actions = []

    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie", "unrelated": "never-send"}

    async def handle(request):
        actions.append(request)
        if str(request.url).startswith(INDEX):
            return httpx.Response(
                302, headers={"location": "https://user.qzone.qq.com/123456?token=not-logged"}
            )
        assert str(request.url).startswith(ACTIVE)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "vFeeds": [
                        {
                            "uin": 777777,
                            "tid": "synthetic-post",
                            "name": "测试作者",
                            "content": "合成动态",
                            "created_time": int(NOW.timestamp()),
                        }
                    ]
                },
            },
        )

    reader = QzoneReader(credentials, transport=httpx.MockTransport(handle))
    account, posts = await reader.fetch()
    assert account == "123456" and posts[0]["body"] == "合成动态"
    assert len(actions) == 2 and all(r.method == "GET" for r in actions)
    assert all("unrelated" not in r.headers.get("cookie", "") for r in actions)
    assert "test-cookie" not in str(posts) + str(reader.trace)
    assert "not-logged" not in str(reader.trace)


async def test_login_redirect_is_not_followed_and_wrong_account_never_reaches_network():
    calls = []

    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie"}

    async def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://ssl.ptlogin2.qq.com/login"})

    reader = QzoneReader(credentials, transport=httpx.MockTransport(handle))
    with pytest.raises(QzoneReadError):
        await reader.fetch()
    assert len(calls) == 1

    async def wrong():
        return "123456", {"uin": "o999999", "p_skey": "test-cookie"}

    with pytest.raises(QzoneReadError):
        await QzoneReader(wrong, transport=httpx.MockTransport(handle)).fetch()
    assert len(calls) == 1


def test_image_and_comment_normalization_does_not_collect_avatar_as_post_photo():
    raw = {
        "code": 0,
        "data": {
            "vFeeds": [
                {
                    "uin": 777777,
                    "tid": "synthetic-post",
                    "content": "合成动态",
                    "pic": [{"url": "https://qpic.cn/photo.png"}],
                    "userinfo": {"avatar": "https://qlogo.cn/avatar.jpg"},
                }
            ]
        },
    }
    value = normalize_posts(raw, "123456")[0]
    assert value["images"] == ["https://qpic.cn/photo.png"]
    assert parse_cookies("uin=o123456; p_skey=x; fake=y") == {"uin": "o123456", "p_skey": "x"}
