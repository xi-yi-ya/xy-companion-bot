"""Local synthetic OneBot transport test; no real QQ messages are sent."""

import asyncio
import json
import time
import uuid
from pathlib import Path

import psycopg
import websockets
from websockets.exceptions import InvalidStatus

from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent


async def main():
    settings = json.loads((ROOT / "runtime/qq/settings.json").read_text("utf-8"))
    if settings.get("owner_qq"):
        raise ValueError("This synthetic test is only for the unpaired, waiting-scan state")
    url = f"ws://127.0.0.1:{settings['ws_port']}/ws"
    headers = {"X-Self-ID": "999000111", "X-Client-Role": "Universal"}
    try:
        async with websockets.connect(
            url,
            additional_headers={
                **headers,
                "Authorization": "Bearer deliberately-invalid-test-token",
            },
            open_timeout=5,
        ):
            raise AssertionError("Invalid OneBot token was accepted")
    except InvalidStatus as exc:
        assert exc.response.status_code in (401, 403)
    marker = "QQ_LOCAL_GUARD_QA_" + uuid.uuid4().hex
    with psycopg.connect(**database_parameters()) as conn:
        calls_before = conn.execute("SELECT count(*) FROM api_usage").fetchone()[0]
    actions = []
    async with websockets.connect(
        url,
        additional_headers={**headers, "Authorization": "Bearer " + settings["ws_token"]},
        open_timeout=5,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": 999000111,
                    "time": int(time.time()),
                }
            )
        )
        for index, kind in enumerate(("private", "group")):
            text = "/help " + marker
            event = {
                "post_type": "message",
                "message_type": kind,
                "sub_type": "friend" if kind == "private" else "normal",
                "self_id": 999000111,
                "time": int(time.time()),
                "user_id": 19999991,
                "message_id": int(time.time()) + index,
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
                "sender": {"user_id": 19999991, "nickname": "Local synthetic test"},
                "font": 0,
            }
            if kind == "group":
                event["group_id"] = 999000222
            await ws.send(json.dumps(event))
        until = time.monotonic() + 3
        while time.monotonic() < until:
            try:
                item = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=until - time.monotonic())
                )
            except TimeoutError:
                break
            if item.get("action"):
                actions.append(item["action"])
                await ws.send(
                    json.dumps({"status": "ok", "retcode": 0, "data": {}, "echo": item.get("echo")})
                )
    with psycopg.connect(**database_parameters()) as conn:
        count = conn.execute(
            "SELECT count(*) FROM messages WHERE content LIKE %s", ("%" + marker + "%",)
        ).fetchone()[0]
        calls_after = conn.execute("SELECT count(*) FROM api_usage").fetchone()[0]
    assert not actions and count == 0 and calls_after == calls_before, (actions, count)
    result = {
        "invalid_token_rejected": True,
        "valid_transport_connected": True,
        "unknown_private_and_group_ignored": True,
        "raw_messages_written": count,
        "model_calls_added": calls_after - calls_before,
        "external_qq_messages_sent": False,
    }
    (ROOT / "runtime/qa/qq-transport-evidence.json").write_text(
        json.dumps(result, indent=2), "utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
