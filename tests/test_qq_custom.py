import asyncio
import base64
import copy
import io

import pytest
from PIL import Image
from test_qq_agent import Harness

from plugin.qq_custom import DEFAULT_CUSTOM, QQCustom, marked_sticker_refs, validate_custom

LABEL = {
    "is_meme": True,
    "sensitive": False,
    "description": "笑脸，表示收到",
    "tags": ["收到", "开心"],
    "emotion": "开心",
}


class CustomHarness(Harness):
    def __init__(self, store, tmp_path):
        super().__init__(store, tmp_path)
        self.labels = 0
        self.service = QQCustom(store.pool, self.agent, self.classify)

    async def classify(self, path):
        self.labels += 1
        return LABEL.copy()

    async def image(self, color="green", approved=False):
        output = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(output, format="PNG")
        return (
            await self.agent.add_asset(
                "smile.png", base64.b64encode(output.getvalue()).decode(), approved=approved
            )
        )["asset_id"]

    async def config(self, **updates):
        value = copy.deepcopy(DEFAULT_CUSTOM)
        value.update(updates)
        return await self.service.save(value)


def test_configuration_and_protocol_marks_are_strict():
    assert validate_custom(DEFAULT_CUSTOM)["modes"]["avatar"] == "confirm"
    with pytest.raises(ValueError):
        validate_custom({**DEFAULT_CUSTOM, "collect_per_hour": True})
    raw = {
        "post_type": "message",
        "message": [{"type": "image", "data": {"url": "https://example.com/photo.png"}}],
    }
    assert not marked_sticker_refs(raw)
    raw["message"][0]["data"]["sub_type"] = 1
    assert marked_sticker_refs(raw) == ["https://example.com/photo.png"]


async def test_nickname_requires_real_approval_and_only_changes_bot(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    proposal = await h.service.propose("nickname", "新名字", "想换一个称呼", 123)
    assert proposal["status"] == "pending" and not h.calls
    result = await h.service.apply(proposal["action_id"], approve=True)
    assert result["status"] == "sent"
    assert h.calls[0] == ("set_qq_profile", {"nickname": "新名字"})
    assert (await h.service.apply(proposal["action_id"], approve=True))["status"] == "sent"
    assert len([c for c in h.calls if c[0] == "set_qq_profile"]) == 1


async def test_auto_signature_idempotency_and_cooldown(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    assert (await h.service.propose("signature", "看看晚霞", "自己的感受", 12))["status"] == "sent"
    assert (await h.service.propose("signature", "看看晚霞", "重入", 12))["status"] == "sent"
    newer = await h.service.propose("signature", "听听风声", "想法", 13)
    assert (
        newer["status"] == "rate_limited"
        and len([c for c in h.calls if c[0] == "set_self_longnick"]) == 1
    )
    assert await h.service.reject(newer["action_id"])


async def test_mode_switch_cannot_sweep_old_pending_or_override_block(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    proposal = await h.service.propose("nickname", "新名字", "候选", 2)
    cfg = copy.deepcopy(DEFAULT_CUSTOM)
    cfg["modes"]["nickname"] = "auto"
    await h.service.save(cfg)
    assert (await h.service.apply(proposal["action_id"]))["status"] == "pending"

    async def block(*args):
        return {"allowed": False, "reason": "包含私人资料"}

    h.service.check = block
    blocked = await h.service.propose("signature", "私人内容", "测试", 3)
    assert blocked["status"] == "blocked"
    assert (await h.service.apply(blocked["action_id"], approve=True))["status"] == "blocked"
    assert not h.calls


async def test_quarantine_until_confirm_and_scope_does_not_expand_on_duplicate(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    cfg = copy.deepcopy(DEFAULT_CUSTOM)
    cfg["modes"]["sync"] = "off"
    await h.service.save(cfg)
    image = await h.image()
    result = await h.service.propose(
        "collect", image, "普通截图", 4, label={**LABEL, "is_meme": False}, scope="group:987654"
    )
    assert result["status"] == "pending" and not await h.agent.assets()
    with pytest.raises(ValueError, match="待审区"):
        await h.agent.send_media(image, "private", "123456", "blocked-image")
    await h.service.apply(result["action_id"], approve=True)
    assert len(await h.agent.assets()) == 1
    assert not await h.service.search("收到", "peer:777777")
    assert len(await h.service.search("收到", "group:987654")) == 1
    duplicate = await h.service.propose("collect", image, "再收录", 5, scope="all")
    assert duplicate["status"] == "already_collected"
    assert (await h.service.search("", "owner"))[0]["scope"] == "group:987654"


async def test_sync_native_error_is_not_success_and_success_readback(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    image = await h.image()

    async def rejected(action, args):
        return {"result": 5} if action == "add_custom_face" else []

    h.hook = rejected
    result = await h.service.propose("collect", image, "表情", 8)
    assert result["status"] == "sent" and result["qq_sync"]["status"] == "failed"

    async def accepted(action, args):
        if action == "add_custom_face":
            return {"result": 0}
        if action == "fetch_custom_face_detail":
            import hashlib

            return [
                {"md5": hashlib.md5((await h.service.asset(image))[1].read_bytes()).hexdigest()}
            ]
        return {}

    h.hook = accepted
    sync = await h.service.propose("sync", image, "重试明确失败", 9)
    assert sync["status"] == "sent" and sync["verified"] is True
    assert (await h.service.propose("sync", image, "又试", 10))["status"] == "already_synced"


async def test_unknown_sync_is_never_added_twice(store, tmp_path):
    h = CustomHarness(store, tmp_path)

    async def lost(action, args):
        raise TimeoutError("native timeout")

    h.hook = lost
    image = await h.image()
    result = await h.service.propose("collect", image, "表情", 10)
    assert result["qq_sync"]["status"] == "unknown"
    assert (await h.service.propose("sync", image, "又试", 11))["status"] == "unknown"
    assert len(h.calls) == 1


async def test_pending_queue_is_terminal_not_reclassified_every_poll(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    image = await h.image()
    source = await store.record("webchat", "owner", "queue:test", "event", "user", "表情")

    async def fetch(refs):
        return {"asset_id": image}

    async def screenshot(path):
        h.labels += 1
        return {**LABEL, "is_meme": False}

    h.service.fetch, h.service.classify = fetch, screenshot
    await h.service.enqueue(
        {
            "post_type": "message",
            "message": [
                {"type": "image", "data": {"sub_type": 1, "url": "https://example.com/test.png"}}
            ],
        },
        source,
    )
    await h.service.step()
    await h.service.step()
    assert h.labels == 1 and not h.calls
    async with store.pool.connection() as conn:
        row = await (await conn.execute("SELECT status FROM qq_sticker_queue")).fetchone()
    assert row["status"] == "awaiting_approval"


async def test_permission_change_while_embedding_cancels_collect(store, tmp_path):
    h = CustomHarness(store, tmp_path)

    class Encoder:
        async def encode(self, texts):
            cfg = copy.deepcopy(DEFAULT_CUSTOM)
            cfg["modes"]["collect"] = "off"
            await h.service.save(cfg)
            return [[0.0] * 512]

    h.service.embedder = Encoder()
    result = await h.service.propose("collect", await h.image(), "候选", 20)
    assert result["status"] == "cancelled" and not h.calls
    assert not await h.agent.assets()


async def test_inflight_profile_permission_change_prevents_native_call(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    original = h.agent.execute

    async def race(*args, **kwargs):
        cfg = copy.deepcopy(DEFAULT_CUSTOM)
        cfg["modes"]["signature"] = "off"
        await h.service.save(cfg)
        return await original(*args, **kwargs)

    h.agent.execute = race
    result = await h.service.propose("signature", "晚安", "测试", 21)
    assert result["status"] == "cancelled" and not h.calls


async def test_parallel_same_image_collects_once_and_capacity_does_not_delete(store, tmp_path):
    h = CustomHarness(store, tmp_path)
    cfg = copy.deepcopy(DEFAULT_CUSTOM)
    cfg["modes"]["sync"] = "off"
    cfg["capacity"] = 1
    await h.service.save(cfg)
    image = await h.image()
    await asyncio.gather(*(h.service.propose("collect", image, "表情", n) for n in (30, 31)))
    assert len(await h.service.search("")) == 1
    result = await h.service.propose("collect", await h.image("red"), "第二张", 32)
    assert result["status"] == "capacity_reached" and len(await h.service.search("")) == 1
