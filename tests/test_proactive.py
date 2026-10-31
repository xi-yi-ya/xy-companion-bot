import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from plugin.persona import build_system_prompt
from plugin.proactive import (
    DEFAULT_CONTACT,
    OWNER,
    ContactService,
    ContactStore,
    next_allowed,
    validate_contact,
)

BASE = datetime(2026, 11, 1, 4, tzinfo=UTC)  # Noon in Beijing.
TARGET = {"umo": "companion_qq:FriendMessage:123", "account": "123", "self_id": "456"}


class Midpoint:
    def randint(self, lo, hi):
        return (lo + hi) // 2


async def user(store, at=BASE, platform="webchat", account="owner", event=None):
    return await store.record(
        platform, account, "test:contact", event or uuid.uuid4().hex, "user", "我们下次继续聊", at
    )


async def setup(store):
    store.contact.rng = Midpoint()
    await store.contact.save({**DEFAULT_CONTACT, "quiet_enabled": False}, BASE)
    return await user(store)


class Harness:
    def __init__(self, store):
        self.now = BASE + timedelta(hours=12)
        self.generations, self.sends = 0, []
        self.online, self.busy, self.persona_current = True, False, True
        self.generate_hook, self.send_hook = None, None
        self.service = ContactService(
            store.contact,
            self.generate,
            self.send,
            self.target,
            lambda: self.busy,
            self.current,
            lambda: self.now,
        )

    async def target(self):
        return TARGET if self.online else None

    async def current(self, result):
        return self.persona_current

    async def generate(self, run):
        self.generations += 1
        if self.generate_hook:
            await self.generate_hook(run)
        return {"send": True, "text": " 想起了你，要不要接着聊？ ", "reason": "承接未完话题"}

    async def send(self, target, text):
        self.sends.append((target, text))
        if self.send_hook:
            return await self.send_hook()
        return {"message_id": 789}


@pytest.mark.parametrize(
    "local,expected",
    [
        ("2026-11-01T23:00+08:00", "2026-11-02T08:00+08:00"),
        ("2026-11-02T00:00+08:00", "2026-11-02T08:00+08:00"),
        ("2026-11-02T07:59+08:00", "2026-11-02T08:00+08:00"),
        ("2026-11-02T08:00+08:00", "2026-11-02T08:00+08:00"),
        ("2026-11-02T22:59+08:00", "2026-11-02T22:59+08:00"),
    ],
)
def test_cross_midnight_quiet(local, expected):
    settings = {**DEFAULT_CONTACT, "quiet_start": "23:00"}
    assert next_allowed(datetime.fromisoformat(local), settings) == datetime.fromisoformat(expected)


def test_daytime_quiet():
    settings = {**DEFAULT_CONTACT, "quiet_start": "12:00", "quiet_end": "14:00"}
    assert next_allowed(BASE, settings) == BASE + timedelta(hours=2)
    settings["quiet_enabled"] = False
    assert next_allowed(BASE, settings) == BASE


@pytest.mark.parametrize(
    "patch",
    [
        {"min_hours": 0},
        {"min_hours": True},
        {"min_hours": float("nan")},
        {"min_hours": 15, "max_hours": 14},
        {"max_hours": 169},
        {"enabled": "true"},
        {"quiet_start": "25:00"},
        {"quiet_end": "00:00"},
        {"unknown": 1},
    ],
)
def test_settings_validation(patch):
    with pytest.raises(ValueError):
        validate_contact({**DEFAULT_CONTACT, **patch})


async def test_persistent_random_schedule_and_duplicate_does_not_reset(store):
    message = await setup(store)
    state = (await store.contact.snapshot())["state"]
    assert state["last_user_id"] == message["id"]
    assert 10 * 3600 <= state["interval_seconds"] <= 14 * 3600
    assert state["due_at"] == BASE + timedelta(hours=12)
    assert await store.contact.claim(BASE + timedelta(hours=10), TARGET) is None
    same = await user(store, at=BASE + timedelta(hours=1), event=message["event_id"])
    assert same["id"] == message["id"]
    other = ContactStore(store.pool)
    await other.seed(BASE + timedelta(hours=1))
    assert (await other.snapshot())["state"] == state
    await store.record("webchat", "owner", "test:contact", "bot", "assistant", "嗯", BASE)
    assert (await other.snapshot())["state"] == state


async def test_qq_and_web_share_owner_timer_other_people_do_not(store):
    await setup(store)
    async with store.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO platform_accounts(platform,account_id,person_id) VALUES('aiocqhttp','123',%s)",
            (OWNER,),
        )
    qq = await user(store, BASE + timedelta(hours=2), "aiocqhttp", "123")
    assert qq["person_id"] == OWNER
    state = (await store.contact.snapshot())["state"]
    assert state["due_at"] == BASE + timedelta(hours=14)
    await user(store, BASE + timedelta(hours=3), account="someone_else")
    assert (await store.contact.snapshot())["state"] == state


async def test_one_message_until_reply_and_only_real_transcript(store):
    source = await setup(store)
    h = Harness(store)
    await h.service.step()
    snap = await store.contact.snapshot()
    assert snap["state"]["awaiting_reply"] and snap["state"]["due_at"] is None
    assert snap["deliveries"][0]["status"] == "sent"
    assert len(h.sends) == 1 and h.sends[0][1].startswith("想起")
    async with store.pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM messages ORDER BY id")).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[-1]["conversation_id"] == TARGET["umo"] and rows[-1]["event_id"].startswith(
        "proactive:"
    )
    for enabled in (False, True):
        await store.contact.save({**snap["settings"], "enabled": enabled}, h.now)
        await h.service.step()
    h.now += timedelta(days=3)
    await h.service.step()
    assert h.generations == 1 and len(h.sends) == 1
    reply = await user(store, h.now)
    fresh = (await store.contact.snapshot())["state"]
    assert fresh["last_user_id"] != source["id"] and fresh["last_user_id"] == reply["id"]
    assert not fresh["awaiting_reply"] and fresh["due_at"] == h.now + timedelta(hours=12)
    h.now = fresh["due_at"]
    await h.service.step()
    assert len(h.sends) == 2


@pytest.mark.parametrize("condition", ["offline", "busy", "disabled", "quiet"])
async def test_no_generation_when_unavailable(store, condition):
    await setup(store)
    h = Harness(store)
    if condition == "offline":
        h.online = False
    elif condition == "busy":
        h.busy = True
    else:
        settings = {**DEFAULT_CONTACT, "enabled": condition != "disabled"}
        await store.contact.save(settings, BASE)
        h.now = BASE + timedelta(hours=13)  # 01:00 Beijing, still quiet.
    await h.service.step()
    assert h.generations == 0 and h.sends == []


@pytest.mark.parametrize("change", ["new_user", "disabled", "persona", "offline"])
async def test_recheck_after_generation_prevents_stale_send(store, change):
    await setup(store)
    h = Harness(store)

    async def changed(run):
        if change == "new_user":
            await user(store, h.now)
        elif change == "disabled":
            await store.contact.save({**DEFAULT_CONTACT, "enabled": False}, h.now)
        elif change == "persona":
            h.persona_current = False
        else:
            h.online = False

    h.generate_hook = changed
    await h.service.step()
    assert h.generations == 1 and not h.sends
    assert (await store.contact.snapshot())["deliveries"][0]["status"] in ("cancelled", "skipped")


@pytest.mark.parametrize("receipt", [None, {}, {"other": 1}, "timeout"])
async def test_uncertain_delivery_never_retries(store, receipt):
    await setup(store)
    h = Harness(store)

    async def uncertain():
        if receipt == "timeout":
            raise TimeoutError()
        return receipt

    h.send_hook = uncertain
    await h.service.step()
    state = await store.contact.snapshot()
    assert state["deliveries"][0]["status"] == "unknown"
    assert state["state"]["awaiting_reply"]
    await store.contact.recover(h.now, startup=True)
    h.now += timedelta(days=2)
    await h.service.step()
    assert len(h.sends) == 1
    async with store.pool.connection() as conn:
        assert (
            await (
                await conn.execute("SELECT count(*) AS n FROM messages WHERE role='assistant'")
            ).fetchone()
        )["n"] == 0


async def test_user_reply_during_network_is_not_overwritten(store):
    await setup(store)
    h = Harness(store)

    async def reply():
        await user(store, h.now + timedelta(seconds=1))
        return {"message_id": 789}

    h.send_hook = reply
    await h.service.step()
    state = (await store.contact.snapshot())["state"]
    assert not state["awaiting_reply"] and state["current_run"] is None
    assert state["due_at"] == h.now + timedelta(hours=12, seconds=1)


async def test_concurrent_workers_claim_only_once(store):
    await setup(store)
    h = Harness(store)
    await asyncio.gather(h.service.step(), h.service.step())
    assert h.generations == 1 and len(h.sends) == 1


@pytest.mark.parametrize("during_send", [False, True])
async def test_shutdown_before_and_during_send(store, during_send):
    await setup(store)
    h = Harness(store)
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    if during_send:
        h.send_hook = blocked
    else:
        h.generate_hook = blocked
    task = asyncio.create_task(h.service.step())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await store.contact.snapshot()
    assert snapshot["deliveries"][0]["status"] == ("unknown" if during_send else "failed")
    assert snapshot["state"]["awaiting_reply"] == during_send
    assert snapshot["state"]["current_run"] is None


async def test_dnd_begins_while_generating(store):
    await setup(store)
    await store.contact.save(DEFAULT_CONTACT, BASE)
    h = Harness(store)
    h.now = BASE + timedelta(hours=11, minutes=59)  # 23:59, just before quiet.
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE proactive_state SET due_at=%s", (h.now,))

    async def crossed_midnight(run):
        h.now += timedelta(minutes=2)

    h.generate_hook = crossed_midnight
    await h.service.step()
    snapshot = await store.contact.snapshot()
    assert not h.sends and snapshot["deliveries"][0]["status"] == "cancelled"
    assert snapshot["state"]["due_at"] == BASE + timedelta(hours=20)  # 08:00


@pytest.mark.parametrize("sending", [False, True])
async def test_restart_recovery(store, sending):
    await setup(store)
    h = Harness(store)
    run = await store.contact.claim(h.now, TARGET)
    if sending:
        assert await store.contact.begin_send(run, await h.generate(run), h.now)
    await store.contact.recover(h.now, startup=True)
    state = await store.contact.snapshot()
    assert state["deliveries"][0]["status"] == ("unknown" if sending else "failed")
    assert state["state"]["awaiting_reply"] == sending
    assert state["state"]["current_run"] is None


async def test_model_failures_are_bounded_and_reply_recovers(store):
    await setup(store)
    h = Harness(store)

    async def broken(run):
        raise ValueError("invalid model result")

    h.generate_hook = broken
    for _ in range(5):
        await h.service.step()
        h.now += timedelta(minutes=6)
    assert h.generations == 3 and not h.sends
    assert (await store.contact.snapshot())["state"]["due_at"] is None
    await user(store, h.now)
    assert (await store.contact.snapshot())["state"]["attempts"] == 0


async def test_model_can_stay_quiet_without_consuming_cycle(store):
    await setup(store)
    h = Harness(store)

    async def quiet(run):
        return {"send": False, "text": "", "reason": "用户说今天要休息"}

    h.service.generate = quiet
    await h.service.step()
    state = await store.contact.snapshot()
    assert not state["state"]["awaiting_reply"] and not h.sends
    assert state["state"]["due_at"] == h.now + timedelta(hours=12)
    assert state["deliveries"][0]["status"] == "skipped"


async def test_startup_overdue_has_grace_period(store):
    await setup(store)
    now = BASE + timedelta(days=3)
    await store.contact.seed(now)
    assert (await store.contact.snapshot())["state"]["due_at"] == now + timedelta(minutes=2)


async def test_contact_uses_latest_full_dialog_and_archive_without_reinforcement(store):
    old = BASE - timedelta(days=9)
    evidence = await user(store, old)

    async def extract(rows, known):
        return [
            {
                "action": "create",
                "content": "我们下次继续聊",
                "kind": "ordinary",
                "message_id": evidence["id"],
                "quote": evidence["content"],
            }
        ]

    await store.consolidate(OWNER, old.date(), extract)
    current = await user(store)
    await store.record(
        "webchat", "owner", "test:contact", "recent_bot", "assistant", "好，我们聊到一半。", BASE
    )
    source, before = await store.contact.latest()
    memory, history = await store.context(
        OWNER, source, before_id=before, at=BASE, purpose="proactive"
    )
    assert source["id"] == current["id"]
    assert history[-2]["role"] == "user" and current["content"] in history[-2]["content"]
    assert history[-1]["role"] == "assistant" and "聊到一半" in history[-1]["content"]
    assert "偶然回忆" in memory
    archived = (await store.memories(OWNER))[0]
    assert archived["status"] == "archived" and archived["last_user_mentioned_at"] == old
    profile = await store.personas.profile()
    assert profile["profile"]["name"] in build_system_prompt(profile["profile"], [], "")
    async with store.pool.connection() as conn:
        context = await (
            await conn.execute("SELECT * FROM context_runs ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert context["message_id"] == current["id"] and context["details"]["purpose"] == "proactive"


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"send": "yes", "text": "x", "reason": "x"},
        {"send": True, "text": " ", "reason": "x"},
        {"send": True, "text": "很长" * 1000, "reason": "x"},
    ],
)
def test_model_output_validation(result):
    with pytest.raises(ValueError):
        ContactService.validate(result)
