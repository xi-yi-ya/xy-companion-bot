"""Exercise the real OneBot pipeline through a local synthetic bot connection.

No real QQ account receives test replies. All outbound actions are ACKed by this
script on its own self_id. Technical records use a dedicated fake group/person.
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import websockets
from psycopg.rows import dict_row

from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent
SELF, USER, GROUP = 990000111, 19999991, 999000222
PERSON = f"qqgroup:companion_qq:{GROUP}:{USER}"


async def main():
    settings = json.loads((ROOT / "runtime/qq/settings.json").read_text("utf-8"))
    marker = "QQ_CHAT_QA_" + uuid.uuid4().hex
    ids, actions, sends, evidence = [], [], [], {}
    base = int(time.time()) % 1_000_000_000 + 1_000_000_000
    quoted_id = base - 100
    quoted = {
        "self_id": SELF,
        "message_id": quoted_id,
        "message_type": "group",
        "group_id": GROUP,
        "user_id": SELF,
        "time": int(time.time()),
        "sender": {"user_id": SELF, "nickname": "Local QA Bot"},
        "message": [{"type": "text", "data": {"text": "技术材料引用代号是 QUOTE-7319。"}}],
    }
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185", trust_env=False, timeout=8
    ) as client:
        qq = (
            await client.get("/api/v1/plugins/extensions/astrbot_plugin_companion_memory/qq")
        ).json()
        assert str(qq.get("bot_qq")) != str(SELF)
    with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
        assert not conn.execute(
            "SELECT 1 FROM messages WHERE person_id=%s", (PERSON,)
        ).fetchone(), "QA group must start without real data"
        owner_state = conn.execute(
            "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
        ).fetchone()
    url = f"ws://127.0.0.1:{settings['ws_port']}/ws"
    async with websockets.connect(
        url,
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
                    assert int(params.get("group_id", 0)) == GROUP, "Unexpected real recipient"
                    sends.append(params)
                    data = {"message_id": base + 500 + len(sends)}
                elif action == "get_msg":
                    data = quoted
                elif action == "get_group_member_info":
                    data = {
                        "user_id": params.get("user_id", USER),
                        "nickname": "Local QA",
                        "card": "",
                        "role": "member",
                    }
                elif action == "get_group_info":
                    data = {"group_id": GROUP, "group_name": "Local QA Group"}
                else:
                    data = {}
                await ws.send(
                    json.dumps(
                        {"status": "ok", "retcode": 0, "data": data, "echo": request.get("echo")}
                    )
                )

        listener = asyncio.create_task(receive())

        async def post(text, extra=None):
            mid = base + len(ids)
            ids.append(str(mid))
            chain = list(extra or []) + [{"type": "text", "data": {"text": text}}]
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "sub_type": "normal",
                        "self_id": SELF,
                        "user_id": USER,
                        "group_id": GROUP,
                        "message_id": mid,
                        "time": int(time.time()),
                        "font": 0,
                        "message": chain,
                        "raw_message": text,
                        "sender": {"user_id": USER, "nickname": "Local QA"},
                    }
                )
            )
            return str(mid)

        async def wait_sends(count):
            async with asyncio.timeout(75):
                while len(sends) < count:
                    if listener.done():
                        await listener
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)

        def segments(sent):
            content = sent["message"]
            return json.loads(content) if isinstance(content, str) else content

        def text_of(sent):
            return "".join(s["data"]["text"] for s in segments(sent) if s["type"] == "text")

        try:
            await post(marker + " 普通群消息")
            await post(marker + " @全体", [{"type": "at", "data": {"qq": "all"}}])
            await post(marker + " 只引用不@", [{"type": "reply", "data": {"id": str(quoted_id)}}])
            await asyncio.sleep(1.5)
            assert not sends
            with psycopg.connect(**database_parameters()) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM messages WHERE person_id=%s", (PERSON,)
                    ).fetchone()[0]
                    == 0
                )
            evidence["ordinary_atall_and_quote_only_ignored"] = True

            started = time.monotonic()
            await post(
                marker + " 技术验收，不是用户事实。请等我下一条说明数字，最后只回答两数之和。",
                [{"type": "at", "data": {"qq": str(SELF)}}],
            )
            await asyncio.sleep(0.15)
            await post(marker + " 第一个数字是 17。")
            await asyncio.sleep(0.15)
            last = await post(marker + " 第二个数字是 25，只回复相加结果。")
            await wait_sends(1)
            assert len(sends) == 1 and "42" in text_of(sends[-1]), "Burst not combined"
            assert time.monotonic() - started >= 1.25
            chain = segments(sends[-1])
            assert any(c["type"] == "reply" and str(c["data"]["id"]) == last for c in chain), chain
            assert any(c["type"] == "at" and str(c["data"]["qq"]) == str(USER) for c in chain), (
                chain
            )
            evidence.update(
                three_messages_one_reply=True,
                quiet_window_elapsed=True,
                reply_quotes_latest_input=True,
                reply_mentions_group_sender=True,
            )

            await post(
                marker + " 技术验收：被引用的那条消息里的英文代号是什么？只给代号。",
                [
                    {"type": "reply", "data": {"id": str(quoted_id)}},
                    {"type": "at", "data": {"qq": str(SELF)}},
                ],
            )
            await wait_sends(2)
            assert len(sends) == 2 and "QUOTE-7319" in text_of(sends[-1]), (
                "Quote not visible to model"
            )
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                rows = conn.execute(
                    "SELECT id,role,content FROM messages WHERE person_id=%s ORDER BY id", (PERSON,)
                ).fetchall()
                assert sum(r["role"] == "user" for r in rows) == 4
                assert sum(r["role"] == "assistant" for r in rows) == 2
                assert all("QUOTE-7319" not in r["content"] for r in rows if r["role"] == "user")
                quote = conn.execute(
                    "SELECT a.kind,a.status FROM message_attachments a JOIN messages m ON m.id=a.message_id WHERE m.person_id=%s",
                    (PERSON,),
                ).fetchall()
                assert any(q["kind"] == "quote" and q["status"] == "ready" for q in quote)
                usage = conn.execute(
                    "SELECT category,model,input_tokens,output_tokens,error FROM api_usage WHERE person_id=%s AND message_id=ANY(%s)",
                    (PERSON, [r["id"] for r in rows]),
                ).fetchall()
                assert len(usage) == 2 and all(
                    r["input_tokens"] and not r["error"] for r in usage
                ), usage
                current = conn.execute(
                    "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
                ).fetchone()
                assert owner_state == current, "Group QA altered private contact schedule"
            evidence.update(
                quoted_text_understood=True,
                four_original_user_rows=True,
                two_assistant_rows=True,
                quote_not_user_fact=True,
                private_contact_unchanged=True,
                actual_usage=usage,
                no_real_qq_recipient=True,
            )
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            with psycopg.connect(**database_parameters()) as conn:
                conn.execute("DELETE FROM context_runs WHERE person_id=%s", (PERSON,))
                conn.execute("DELETE FROM messages WHERE person_id=%s", (PERSON,))
                conn.execute("DELETE FROM platform_accounts WHERE person_id=%s", (PERSON,))
                conn.execute("DELETE FROM persons WHERE id=%s", (PERSON,))
            evidence["technical_pg_messages_removed"] = True
            (ROOT / "runtime/qa/qq-chat-live-evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
            )
    print(json.dumps(evidence, ensure_ascii=False, default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
