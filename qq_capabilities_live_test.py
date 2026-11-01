"""Owner-authorized real tools check; delete only this run's technical Web chats."""

import base64
import io
import json
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from local_secrets import database_parameters
from plugin.proactive import next_allowed

API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"


def main():
    sessions, ids, evidence = [], [], []
    started = datetime.now(UTC)
    with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
        original = conn.execute(
            "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
        ).fetchone()
    with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=240) as client:

        def chat(prompt):
            session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
            sessions.append(session)
            result = client.post(
                "/api/chat/send",
                json={
                    "session_id": session,
                    "message": "这是功能技术验收，不把测试内容记成用户事实。" + prompt,
                },
            )
            result.raise_for_status()
            events = [
                json.loads(line[6:])
                for line in result.text.splitlines()
                if line.startswith("data: ")
            ]
            complete = [
                e["data"]
                for e in events
                if e.get("type") == "complete" and isinstance(e.get("data"), str) and e["data"]
            ]
            plain = [
                e["data"]
                for e in events
                if e.get("type") == "plain"
                and isinstance(e.get("data"), str)
                and e.get("chain_type") not in ("reasoning", "tool_call", "tool_call_result")
            ]
            text = complete[-1] if complete else "".join(plain)
            assert text and not any(
                word in text
                for word in (
                    "PoolClosed",
                    "All chat models failed",
                    "LLM 响应错误",
                    "<tool_call>",
                    "DSML",
                )
            ), text
            return text

        try:
            for _ in range(23):
                if client.get(API + "qq").json()["online"]:
                    break
                time.sleep(2)
            assert client.get(API + "qq").json()["online"], "QQ 未在等待期间恢复在线"
            if "--image-only" not in sys.argv:
                directory_check = chat(
                    "请实际调用 companion_qq_directory，kind=friends，查一次当前真实好友列表。不要使用旧记录，只报告实际接口返回的好友数量，不发消息。"
                )
                print(
                    json.dumps(
                        {"case": "directory_precheck", "reply": directory_check}, ensure_ascii=False
                    ),
                    flush=True,
                )
                text = chat(
                    "请实际调用 companion_qzone_read 读取我绑定主号的 QQ 空间，最多 3 条。不要发消息、点赞、评论、发布或改设定。按实际返回说明读取结果；空列表或错误就如实说。"
                )
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    runs = conn.execute(
                        "SELECT status,post_count,error FROM qzone_runs WHERE started_at>=%s ORDER BY started_at",
                        (started,),
                    ).fetchall()
                evidence.append({"case": "qzone_read", "reply": text, "runs": runs})
                print(
                    json.dumps(
                        {"case": "qzone_read", "runs": runs, "reply": text}, ensure_ascii=False
                    ),
                    flush=True,
                )
                assert runs, "没有执行实际空间读取"
                text = chat(
                    "请实际调用 companion_qq_directory 查看你现在的好友列表和群列表，只报告数量以及是否查到了真实账号信息，不发送消息、不处理申请。"
                )
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    reads = conn.execute(
                        "SELECT action,status,metadata,error FROM qq_operations WHERE created_at>=%s AND action IN ('get_friend_list','get_group_list')",
                        (started,),
                    ).fetchall()
                assert {r["action"] for r in reads if r["status"] == "read"} == {
                    "get_friend_list",
                    "get_group_list",
                }, reads
                evidence.append({"case": "qq_directory", "reply": text, "operations": reads})
                print(
                    json.dumps(
                        {"case": "qq_directory", "passed": True, "reply": text}, ensure_ascii=False
                    ),
                    flush=True,
                )
                text = chat(
                    "请依次实际调用 companion_qq_create_file 创建‘QQ功能验收.txt’，内容仅为‘这是一份 QQ 文件发送与撤回的技术验收文件。’，然后调用 companion_qq_send_media 发给绑定主号，拿到 sent 回执后立即调用 companion_qq_recall 撤回刚发送的文件。只操作这一份文件，不向其他账号或群发送。"
                )
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    sends = conn.execute(
                        "SELECT id,status,message_id,recalled_at,error FROM qq_operations WHERE created_at>=%s AND action='send_private_msg'",
                        (started,),
                    ).fetchall()
                assert any(r["status"] == "sent" and r["message_id"] for r in sends), {
                    "reply": text,
                    "sends": sends,
                }
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    recalls = conn.execute(
                        "SELECT status,error FROM qq_operations WHERE created_at>=%s AND action='delete_msg'",
                        (started,),
                    ).fetchall()
                assert recalls and all(
                    r["status"] in ("sent", "failed", "unknown") for r in recalls
                )
                evidence.append(
                    {
                        "case": "qq_file_send_recall",
                        "reply": text,
                        "operations": sends,
                        "recall_attempts": recalls,
                    }
                )
                print(
                    json.dumps(
                        {"case": "qq_file_send_recall", "passed": True, "reply": text},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            from PIL import Image, ImageDraw

            picture = Image.new("RGB", (320, 120), "#f5f6f0")
            ImageDraw.Draw(picture).text((20, 45), "QQ image delivery test", fill="#305640")
            output = io.BytesIO()
            picture.save(output, format="PNG")
            asset = client.post(
                API + "qq/agent/assets",
                json={
                    "name": "qq-image-test.png",
                    "data": base64.b64encode(output.getvalue()).decode(),
                },
            ).json()
            image_start = datetime.now(UTC)
            text = chat(
                "请实际调用 companion_qq_send_media，把素材 "
                + asset["asset_id"]
                + " 作为图片发给绑定主号；拿到 sent 回执后立即尝试 companion_qq_recall 撤回。只发送这一张技术验收图片，不发送其他素材。"
            )
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                images = conn.execute(
                    "SELECT id,status,message_id,recalled_at,error FROM qq_operations WHERE created_at>=%s AND metadata->>'asset_id'=%s",
                    (image_start, asset["asset_id"]),
                ).fetchall()
            assert any(r["status"] == "sent" and r["message_id"] for r in images), {
                "reply": text,
                "images": images,
            }
            evidence.append({"case": "qq_image_send_recall", "reply": text, "operations": images})
            print(
                json.dumps(
                    {"case": "qq_image_send_recall", "passed": True, "reply": text},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            Path("runtime/qa/qq-capabilities-live.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
            )
            for session in sessions:
                client.delete("/api/v1/chat/sessions/" + session)
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('companion:proactive:owner',0))"
                )
                for session in sessions:
                    ids.extend(
                        r["id"]
                        for r in conn.execute(
                            "SELECT id FROM messages WHERE conversation_id LIKE %s",
                            ("%" + session,),
                        ).fetchall()
                    )
                current = conn.execute(
                    "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
                ).fetchone()
                conn.execute("DELETE FROM context_runs WHERE message_id=ANY(%s)", (ids,))
                conn.execute("DELETE FROM messages WHERE id=ANY(%s)", (ids,))
                if current and current["last_user_id"] in ids:
                    latest = conn.execute(
                        "SELECT id,created_at FROM messages WHERE person_id='webchat:owner' AND role='user' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if original and latest and original["last_user_id"] == latest["id"]:
                        fields = [key for key in original if key != "person_id"]
                        statement = sql.SQL(
                            "UPDATE proactive_state SET {} WHERE person_id='webchat:owner'"
                        ).format(
                            sql.SQL(",").join(
                                sql.SQL("{}=%s").format(sql.Identifier(k)) for k in fields
                            )
                        )
                        conn.execute(statement, tuple(original[k] for k in fields))
                    elif latest:
                        settings = conn.execute(
                            "SELECT payload FROM proactive_settings WHERE id=true"
                        ).fetchone()["payload"]
                        seconds = random.randint(
                            round(settings["min_hours"] * 3600), round(settings["max_hours"] * 3600)
                        )
                        conn.execute(
                            "UPDATE proactive_state SET last_user_id=%s,last_user_at=%s,due_at=%s,interval_seconds=%s,awaiting_reply=false,current_run=NULL,attempts=0 WHERE person_id='webchat:owner'",
                            (
                                latest["id"],
                                latest["created_at"],
                                next_allowed(
                                    latest["created_at"] + timedelta(seconds=seconds), settings
                                ),
                                seconds,
                            ),
                        )
                    else:
                        conn.execute("DELETE FROM proactive_state WHERE person_id='webchat:owner'")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
