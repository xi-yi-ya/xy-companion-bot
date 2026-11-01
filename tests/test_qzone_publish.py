import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from plugin.qzone_publish import (
    DEFAULT_PUBLICATION,
    PublicationService,
    draft_result,
    fingerprint,
    public_text_error,
    validate_publication,
)
from plugin.qzone_publish_http import PUBLISH, PublicationRejected, QzonePublisher

NOW = datetime(2026, 11, 1, 4, tzinfo=UTC)
TEXTS = [
    "有些话不急着说完，慢一点也好。",
    "忽然觉得蓝色适合安静的傍晚。",
    "比起标准答案，我更喜欢一个好问题。",
    "偶尔留点空白，也挺自在。",
]


class Harness:
    def __init__(self, store):
        self.store, self.now, self.account = store, NOW, "123456"
        self.busy, self.online, self.current, self.allowed, self.wants = (
            False,
            True,
            True,
            True,
            True,
        )
        self.generated, self.checked, self.prepared, self.sent = 0, 0, 0, []
        self.prepare_hook, self.send_hook = None, None
        self.text = None
        self.service = PublicationService(
            store.publications,
            self.generate,
            self.check,
            self.prepare,
            self.send,
            self.available,
            self.persona_current,
            lambda: self.busy,
            lambda: self.now,
        )

    async def enable(self, **kwargs):
        await self.store.publications.save(
            {**DEFAULT_PUBLICATION, "enabled": True, **kwargs}, self.now
        )
        return self

    async def available(self):
        return self.account if self.online else None

    async def generate(self, account):
        self.generated += 1
        return {
            "publish": self.wants,
            "text": self.text or TEXTS[(self.generated - 1) % len(TEXTS)],
            "reason": "想公开表达这个念头",
            "persona_version": (await self.store.personas.profile())["version"],
            "public_traits_hash": "test",
        }

    async def check(self, account, draft):
        self.checked += 1
        return {"allowed": self.allowed, "reason": "检查结果"}

    async def prepare(self, account):
        self.prepared += 1
        if self.prepare_hook:
            await self.prepare_hook()
        return account, {}

    async def send(self, prepared, text):
        self.sent.append(text)
        if self.send_hook:
            return await self.send_hook()
        return {
            "post_id": "posted-" + str(len(self.sent)),
            "url": f"https://user.qzone.qq.com/{self.account}/mood/posted-{len(self.sent)}",
        }

    async def persona_current(self, draft):
        return self.current


async def test_disabled_default_does_not_generate_or_post(store):
    h = Harness(store)
    assert (await h.service.step(force=True))["status"] == "waiting"
    assert h.generated == 0 and h.sent == []


async def test_auto_first_check_is_scheduled_and_empty_time_does_not_fill_quota(store):
    h = await Harness(store).enable()
    assert (await h.service.step())["status"] == "waiting"
    snapshot = await store.publications.snapshot(h.account, h.now)
    assert (
        NOW + timedelta(minutes=15)
        <= snapshot["state"]["next_consider_at"]
        <= NOW + timedelta(minutes=30)
    )
    assert h.generated == 0
    h.now = snapshot["state"]["next_consider_at"]
    h.wants = False
    assert (await h.service.step())["status"] == "skipped"
    assert h.checked == 0 and h.prepared == 0 and h.sent == []
    assert (await store.publications.snapshot(h.account, h.now))["rate"]["used_today"] == 0


async def test_minimum_interval_cannot_be_bypassed_by_manual_consider(store):
    h = await Harness(store).enable()
    assert (await h.service.step(force=True))["status"] == "sent"
    h.now += timedelta(hours=11, minutes=59)
    assert (await h.service.step(force=True))["status"] == "waiting"
    assert h.generated == 1 and len(h.sent) == 1
    h.now += timedelta(minutes=1)
    assert (await h.service.step(force=True))["status"] == "sent"


async def test_daily_limit_is_beijing_calendar_and_interval_survives_midnight(store):
    h = Harness(store)
    h.now = datetime(2026, 11, 1, 15, 50, tzinfo=UTC)  # Beijing 23:50.
    await h.enable(min_hours=0.25, daily_limit=1)
    assert (await h.service.step(force=True))["status"] == "sent"
    h.now += timedelta(minutes=10)
    assert (await store.publications.snapshot(h.account, h.now))["rate"]["used_today"] == 0
    assert (await h.service.step(force=True))["status"] == "waiting"
    h.now += timedelta(minutes=5)
    assert (await h.service.step(force=True))["status"] == "sent"
    h.now += timedelta(hours=1)
    assert (await h.service.step(force=True))["status"] == "waiting"


async def test_publication_limits_are_scoped_to_current_bot_account(store):
    h = await Harness(store).enable(daily_limit=1)
    assert (await h.service.step(force=True))["status"] == "sent"
    h.account = "654321"
    assert (await h.service.step(force=True))["status"] == "sent"


async def test_checker_block_and_changed_persona_do_not_reach_publish(store):
    h = await Harness(store).enable()
    h.allowed = False
    assert (await h.service.step(force=True))["status"] == "blocked"
    assert h.prepared == 0
    h.allowed, h.current = True, False
    assert (await h.service.step(force=True))["status"] == "cancelled"
    assert h.sent == []


async def test_disable_or_change_limit_after_generation_is_checked_in_transaction(store):
    h = await Harness(store).enable()

    async def disable():
        await store.publications.save({**DEFAULT_PUBLICATION, "enabled": False}, h.now)

    h.prepare_hook = disable
    assert (await h.service.step(force=True))["status"] == "cancelled"
    assert not h.sent


async def test_duplicate_text_is_blocked_even_if_content_checker_allows_it(store):
    h = await Harness(store).enable()
    h.text = TEXTS[0]
    assert (await h.service.step(force=True))["status"] == "sent"
    h.now += timedelta(hours=13)
    assert (await h.service.step(force=True))["status"] == "cancelled"
    assert len(h.sent) == 1
    assert fingerprint("ＡＢＣ！你好") == fingerprint("abc 你好。")


async def test_unknown_result_consumes_interval_and_daily_quota_without_retry(store):
    h = await Harness(store).enable()

    async def unknown():
        raise TimeoutError("ACK lost")

    h.send_hook = unknown
    assert (await h.service.step(force=True))["status"] == "unknown"
    assert (await h.service.step(force=True))["status"] == "waiting"
    assert len(h.sent) == 1
    snapshot = await store.publications.snapshot(h.account, h.now)
    assert (
        snapshot["rate"]["used_today"] == 1 and snapshot["publications"][0]["status"] == "unknown"
    )


async def test_definite_server_rejection_never_reports_success_and_stops_after_three(store):
    h = await Harness(store).enable()

    async def reject():
        raise PublicationRejected("空间接口拒绝发布（代码 -3000）")

    h.send_hook = reject
    for _ in range(3):
        assert (await h.service.step(force=True))["status"] == "failed"
        h.now += timedelta(hours=3)
    assert (await h.service.step())["status"] == "waiting"
    assert len(h.sent) == 3
    await h.enable()
    assert (await store.publications.snapshot(h.account, h.now))["state"][
        "consecutive_failures"
    ] == 0


@pytest.mark.parametrize("during_send", [False, True])
async def test_reload_waits_for_manual_job_and_keeps_uncertain_send_reserved(store, during_send):
    h = await Harness(store).enable()
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    if during_send:
        h.send_hook = blocked
    else:
        h.prepare_hook = blocked
    task = asyncio.create_task(h.service.step(force=True))
    await entered.wait()
    await h.service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await store.publications.snapshot(h.account, h.now)
    assert snapshot["state"]["current_run"] is None
    assert snapshot["publications"][0]["status"] == ("unknown" if during_send else "cancelled")
    assert snapshot["rate"]["used_today"] == int(during_send)


async def test_crash_after_reservation_is_recovered_as_unknown_not_reposted(store):
    h = await Harness(store).enable()
    run = await store.publications.claim(h.account, h.now, force=True)
    assert await store.publications.begin_send(run, await h.generate(h.account), h.now)
    h.now += timedelta(minutes=6)
    assert (await h.service.step(force=True))["status"] == "waiting"
    assert h.sent == []
    assert (await store.publications.snapshot(h.account, h.now))["publications"][0][
        "status"
    ] == "unknown"


async def test_publish_does_not_create_user_facts_or_reset_private_contact(store):
    user = await store.record("webchat", "owner", "qa:public", "one", "user", "技术会话")
    before = (await store.contact.snapshot())["state"]
    h = await Harness(store).enable()
    assert (await h.service.step(force=True))["status"] == "sent"
    assert (await store.contact.snapshot())["state"] == before
    assert await store.memories(user["person_id"]) == []


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": "true"},
        {"min_hours": True},
        {"min_hours": 0},
        {"daily_limit": 0},
        {"daily_limit": 1.5},
    ],
)
def test_publication_settings_reject_invalid_values(change):
    with pytest.raises(ValueError):
        validate_publication({**DEFAULT_PUBLICATION, **change})


@pytest.mark.parametrize(
    "text",
    [
        "联系我 13812345678",
        "<tool_call>internal</tool_call>",
        "p_skey=not-a-cookie",
        "https://example.com",
        "x@example.com",
    ],
)
def test_public_text_rejects_internal_markup_credentials_and_contact_data(text):
    assert public_text_error(text, [])


def test_writer_can_choose_silence_but_not_empty_or_oversized_publication():
    assert not draft_result({"publish": False, "text": "", "reason": "没有新的想法"})["publish"]
    with pytest.raises(ValueError):
        draft_result({"publish": True, "text": "", "reason": "x"})


async def test_http_sends_one_own_account_text_post_and_returns_real_id():
    requests = []

    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie"}

    async def handle(request):
        requests.append(request)
        assert request.method == "POST" and str(request.url).startswith(PUBLISH)
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        assert data["hostuin"] == ["123456"] and data["con"] == [TEXTS[0]]
        assert data["issyncweibo"] == ["0"] and data["richval"] == [""]
        return httpx.Response(200, json={"code": 0, "data": {"tid": "new-status-1"}})

    publisher = QzonePublisher(credentials, transport=httpx.MockTransport(handle))
    result = await publisher.send(await publisher.prepare("123456"), TEXTS[0])
    assert result == {
        "post_id": "new-status-1",
        "url": "https://user.qzone.qq.com/123456/mood/new-status-1",
    }
    assert len(requests) == 1 and "test-cookie" not in str(result)


@pytest.mark.parametrize(
    "body,error", [({"code": -3000}, PublicationRejected), ({"code": 0}, ValueError)]
)
async def test_http_never_retries_rejected_or_receiptless_post(body, error):
    requests = []

    async def credentials():
        return "123456", {"uin": "o123456", "p_skey": "test-cookie"}

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    publisher = QzonePublisher(credentials, transport=httpx.MockTransport(handle))
    with pytest.raises(error):
        await publisher.send(await publisher.prepare("123456"), TEXTS[0])
    assert len(requests) == 1
