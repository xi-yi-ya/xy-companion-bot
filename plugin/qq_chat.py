"""QQ admission, quiet-window batching and isolated group identities."""

import asyncio
import copy
import time
from dataclasses import dataclass, field

from .attachments import component_kind, filename


def chat_key(event):
    return (
        event.get_platform_id(),
        str(event.get_self_id()),
        "private" if event.is_private_chat() else str(event.get_group_id()),
        str(event.get_sender_id()),
    )


def directly_mentioned(event):
    return any(
        component_kind(c) == "at" and str(getattr(c, "qq", "")) == str(event.get_self_id())
        for c in event.get_messages()
    )


def record_platform(event):
    if event.get_platform_name() == "aiocqhttp" and not event.is_private_chat():
        return f"qqgroup:{event.get_platform_id()}:{event.get_group_id()}"
    return event.get_platform_name()


def raw_content(event):
    labels = [
        f"[用户发送附件：{filename(getattr(c, 'name', None) or ('图片' if component_kind(c) == 'image' else '附件'))}]"
        for c in event.get_messages()
        if component_kind(c) in ("image", "file")
    ]
    labels.extend(
        f"[用户引用消息 #{c.id}]" for c in event.get_messages() if component_kind(c) == "reply"
    )
    return event.message_str + ("\n" + "\n".join(labels) if labels else "")


def meaningful(event):
    return bool(event.message_str.strip()) or any(
        component_kind(c) in ("image", "file", "json", "reply", "record", "video")
        for c in event.get_messages()
    )


def snapshot(event):
    value = copy.copy(event)
    value.message_obj = copy.copy(event.message_obj)
    value.message_obj.message = list(event.get_messages())
    return value


@dataclass
class Input:
    event: object
    future: asyncio.Future
    row: dict | None = None
    dropped: bool = False


@dataclass
class Burst:
    key: tuple
    first: float
    last: float
    entries: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    sealed: bool = False


class QuietWindow:
    def __init__(self, quiet=1.0, maximum=5.0):
        self.quiet, self.maximum = quiet, maximum
        self.pending, self.tasks = {}, set()

    def collecting(self, key):
        burst = self.pending.get(key)
        return bool(
            burst
            and not burst.sealed
            and time.monotonic() < min(burst.last + self.quiet, burst.first + self.maximum)
        )

    async def collect(self, event, capture):
        key, now = chat_key(event), time.monotonic()
        burst = self.pending.get(key)
        if (
            not burst
            or burst.sealed
            or now >= min(burst.last + self.quiet, burst.first + self.maximum)
        ):
            burst = Burst(key, now, now)
            self.pending[key] = burst
            task = asyncio.create_task(self.flush(burst))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        entry = Input(snapshot(event), asyncio.get_running_loop().create_future())
        burst.entries.append(entry)
        burst.last = now
        burst.changed.set()
        try:
            entry.row = await capture(entry.event)
            burst.changed.set()
            return await entry.future
        finally:
            if entry.row is None or entry.future.cancelled():
                entry.dropped = True
                burst.changed.set()

    async def flush(self, burst):
        try:
            while True:
                burst.changed.clear()
                now = time.monotonic()
                deadline = min(burst.last + self.quiet, burst.first + self.maximum)
                if now >= deadline:
                    burst.sealed = True
                    active = [e for e in burst.entries if not e.dropped]
                    if all(e.row is not None for e in active):
                        if self.pending.get(burst.key) is burst:
                            self.pending.pop(burst.key)
                        for entry in active:
                            if not entry.future.done():
                                entry.future.set_result(active if entry is active[-1] else None)
                        return
                try:
                    await asyncio.wait_for(
                        burst.changed.wait(),
                        timeout=max(0.01, deadline - now) if not burst.sealed else 10,
                    )
                except TimeoutError:
                    pass
        finally:
            if self.pending.get(burst.key) is burst:
                self.pending.pop(burst.key)
            for entry in burst.entries:
                if not entry.future.done():
                    entry.future.cancel()

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)


class QQIngress:
    def __init__(self, bridge, capture, quiet=1.0, maximum=5.0):
        self.bridge, self.capture = bridge, capture
        self.window = QuietWindow(quiet, maximum)

    async def authorize(self, event):
        if event.is_private_chat():
            return await self.bridge.authorize(str(event.get_sender_id()), event.message_str)
        settings = self.bridge.settings
        if not settings.get("enabled") or not settings.get("owner_qq") or not event.get_group_id():
            return "ignore"
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return "ignore"
        return (
            "allow"
            if directly_mentioned(event) or self.window.collecting(chat_key(event))
            else "ignore"
        )

    async def process(self, event):
        if not meaningful(event) and directly_mentioned(event):
            event.message_str = "[用户@了Bot]"
        # Keep framework commands responsive and out of an ordinary-chat burst.
        if event.message_str.lstrip().startswith(("/", "!")) or not meaningful(event):
            return True
        entries = await self.window.collect(event, self.capture)
        if not entries:
            event.stop_event()
            return False
        event.set_extra("companion_batch", entries)
        event.set_extra("companion_message", entries[-1].row)
        event.set_extra("companion_before_id", min(e.row["id"] for e in entries))
        event.message_obj.message = [c for entry in entries for c in entry.event.get_messages()]
        event.message_str = batch_prompt(entries)
        return True

    async def close(self):
        await self.window.close()


def batch_prompt(entries):
    if len(entries) == 1:
        return entries[0].event.message_str
    return "\n".join(
        f"[用户消息 #{e.row['id']} · {e.row['created_at'].isoformat(timespec='seconds')}]\n{e.event.message_str or '[附件/引用消息]'}"
        for e in entries
    )
