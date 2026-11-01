"""Real AstrBot/DeepSeek validation; a synthetic OneBot receives every reply."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import psycopg
import websockets
from psycopg.rows import dict_row

from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent
SELF, USER, OTHER, GROUP = 990000112, 19999992, 19999993, 999000223
PERSON = f"qqgroup:companion_qq:{GROUP}:room:{SELF}"
API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"


def query(sql, args=()):
    with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
        return conn.execute(sql, args).fetchall()


async def main():
    settings = json.loads((ROOT / "runtime/qq/settings.json").read_text("utf-8"))
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185", trust_env=False, timeout=10
    ) as client:
        options = (await client.get(API + "qq/chat")).json()["settings"]
        name = (await client.get(API + "status")).json()["bot_name"]
    assert options["group_scan_enabled"] and options["quote_mode"] == "adaptive"
    assert not options["group_ids"] or str(GROUP) in options["group_ids"], (
        "QA group not enabled; do not change real settings automatically"
    )
    assert not query("SELECT 1 FROM messages WHERE person_id=%s", (PERSON,))
    base = int(time.time()) % 1_000_000_000 + 1_000_000_000
    sent, ids, actions = [], [], []
    code = "GROUPSCAN-" + str(base)
    evidence = {}
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{settings['ws_port']}/ws",
            additional_headers={
                "X-Self-ID": str(SELF),
                "X-Client-Role": "Universal",
                "Authorization": "Bearer " + settings["ws_token"],
            },
        ) as ws:

            async def receive():
                async for text in ws:
                    request = json.loads(text)
                    if not request.get("action"):
                        continue
                    action, params = request["action"], request.get("params", {})
                    actions.append(action)
                    if action in ("send_group_msg", "send_msg"):
                        assert int(params.get("group_id", 0)) == GROUP, "Unexpected recipient"
                        sent.append(params)
                        data = {"message_id": base + 500 + len(sent)}
                    elif action == "get_group_member_info":
                        data = {
                            "user_id": params.get("user_id", USER),
                            "nickname": "QA member",
                            "card": "",
                            "role": "member",
                        }
                    elif action == "get_group_info":
                        data = {"group_id": GROUP, "group_name": "Local isolated QA"}
                    else:
                        data = {}
                    await ws.send(
                        json.dumps(
                            {
                                "status": "ok",
                                "retcode": 0,
                                "data": data,
                                "echo": request.get("echo"),
                            }
                        )
                    )

            listener = asyncio.create_task(receive())

            async def post(text, user=USER):
                mid = base + len(ids)
                ids.append(str(mid))
                await ws.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "message_type": "group",
                            "sub_type": "normal",
                            "self_id": SELF,
                            "user_id": user,
                            "group_id": GROUP,
                            "message_id": mid,
                            "time": int(time.time()),
                            "font": 0,
                            "message": [{"type": "text", "data": {"text": text}}],
                            "raw_message": text,
                            "sender": {
                                "user_id": user,
                                "nickname": "QA甲" if user == USER else "QA乙",
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                return mid

            async def wait_scan(mid, timeout=75):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if listener.done():
                        listener.result()
                    rows = query(
                        "SELECT s.* FROM qq_group_scans s JOIN messages m ON m.id=s.message_id WHERE m.person_id=%s AND m.event_id=%s ORDER BY s.id DESC LIMIT 1",
                        (PERSON, str(mid)),
                    )
                    if rows:
                        return rows[0]
                    await asyncio.sleep(0.2)
                raise AssertionError("No scan decision")

            async def wait_sent(count):
                deadline = time.monotonic() + 90
                while len(sent) < count and time.monotonic() < deadline:
                    if listener.done():
                        listener.result()
                    await asyncio.sleep(0.2)
                assert len(sent) == count, f"Expected {count} replies, got {len(sent)}"

            idle = await post("QA甲对QA乙说：文件已收到，这只是我俩的交接，不需要其他人回复。")
            scan = await wait_scan(idle)
            assert scan["decision"] == "silent", scan
            await asyncio.sleep(0.3)
            assert not sent
            evidence["silence"] = scan["reason"]

            await post(f"{name}，我给今天这件事取了个代号：{code}。")
            last = await post(f"{name}，甲刚才给这件事取的代号是什么？我没看清前面那一条。", OTHER)
            assert (await wait_scan(last))["decision"] == "reply"
            await wait_sent(1)
            chain = sent[0]["message"]
            assert code in str(chain), chain
            assert not any(c["type"] in ("reply", "at") for c in chain), chain
            evidence["multiple_speakers_no_at"] = chain

            target = await post(f"{name}，接着刚才的事，我暂定明天下午再讨论这个代号对应的计划。")
            last = await post(
                f"{name}，你可以引用甲刚发的那条时间安排说说吗？这样我知道你指的是哪一条。", OTHER
            )
            assert (await wait_scan(last))["decision"] == "reply"
            await wait_sent(2)
            chain = sent[1]["message"]
            assert "<tool_call>" not in str(chain) and "DSML" not in str(chain), chain
            quotes = [c for c in chain if c["type"] == "reply"]
            assert all(str(c["data"]["id"]) in (str(target), str(last)) for c in quotes), chain
            evidence["optional_quote"] = {"selected": bool(quotes), "chain": chain}
            await asyncio.sleep(0.5)
            rows = query(
                "SELECT role,content,id FROM messages WHERE person_id=%s ORDER BY id", (PERSON,)
            )
            assert sum(r["role"] == "user" for r in rows) == 5
            assert sum(r["role"] == "assistant" for r in rows) == 2
            users = [r["content"] for r in rows if r["role"] == "user"]
            assert any(str(OTHER) in x for x in users) and any(str(USER) in x for x in users)
            evidence["records"] = {"users": 5, "assistant": 2, "speakers_preserved": True}
            evidence["usage"] = query(
                "SELECT category,count(*) AS calls FROM api_usage WHERE person_id=%s AND message_id=ANY(%s) GROUP BY category",
                (PERSON, [r["id"] for r in rows if r["role"] == "user"]),
            )
            before = query("SELECT count(*) AS n FROM api_usage WHERE person_id=%s", (PERSON,))[0][
                "n"
            ]
            await asyncio.sleep(options["group_scan_interval"] + 0.4)
            assert (
                before
                == query("SELECT count(*) AS n FROM api_usage WHERE person_id=%s", (PERSON,))[0][
                    "n"
                ]
            )
            assert len(sent) == 2
            evidence["empty_tick_has_no_call"] = True
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        path = ROOT / "runtime/qa/qq-group-scan-live.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8")
        print(
            json.dumps(
                {"passed": True, "evidence": str(path), **evidence}, ensure_ascii=False, default=str
            )
        )
    finally:
        diagnostic = ROOT / "runtime/qa/qq-group-scan-last-attempt.json"
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_text(
            json.dumps(
                {
                    "received_ids": ids,
                    "sends": sent,
                    "evidence": evidence,
                    "rows": query(
                        "SELECT id,event_id,role,content FROM messages WHERE person_id=%s ORDER BY id",
                        (PERSON,),
                    ),
                    "scans": query(
                        "SELECT * FROM qq_group_scans WHERE person_id=%s ORDER BY id", (PERSON,)
                    ),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            "utf-8",
        )
        with psycopg.connect(**database_parameters()) as conn:
            conn.execute("DELETE FROM context_runs WHERE person_id=%s", (PERSON,))
            conn.execute(
                "DELETE FROM message_attachments WHERE message_id IN (SELECT id FROM messages WHERE person_id=%s)",
                (PERSON,),
            )
            conn.execute("DELETE FROM qq_group_scans WHERE person_id=%s", (PERSON,))
            conn.execute("DELETE FROM messages WHERE person_id=%s", (PERSON,))
            conn.execute("DELETE FROM platform_accounts WHERE person_id=%s", (PERSON,))
            conn.execute("DELETE FROM persons WHERE id=%s", (PERSON,))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
