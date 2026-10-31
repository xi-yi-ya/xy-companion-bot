"""Actual in-flight work, without storing prompts or private message content."""

import uuid
from contextlib import contextmanager


class ActivityTracker:
    def __init__(self):
        self.active = {}

    def begin(self, label, task=None):
        token = uuid.uuid4().hex
        callback = None
        if task is not None:

            def callback(done):
                self.end(token)

            task.add_done_callback(callback)
        self.active[token] = (label, task, callback)
        return token

    def end(self, token):
        value = self.active.pop(token, None)
        if value and value[1] is not None and value[2] is not None:
            value[1].remove_done_callback(value[2])

    @contextmanager
    def working(self, label):
        token = self.begin(label)
        try:
            yield
        finally:
            self.end(token)

    def snapshot(self):
        # A cancelled/failed chat is no longer busy even if its final hook did
        # not run. There is no timer guessing whether work should have ended.
        for token, (_, task, _) in list(self.active.items()):
            if task is not None and task.done():
                self.end(token)
        values = list(self.active.values())
        return {"busy": bool(values), "activity": values[-1][0] if values else ""}
