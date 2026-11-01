"""Two real plugin reloads and chats; clean only the technical sessions."""

import json
import random
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from local_secrets import database_parameters
from plugin.proactive import next_allowed

API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"


def main():
    sessions, ids, evidence = [], [], []
    with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
        original = conn.execute(
            "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
        ).fetchone()
    with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=150) as client:
        try:
            for index in range(2):
                reload = client.post(
                    "/api/v1/plugins/astrbot_plugin_companion_memory/reload", json={}
                ).json()
                assert reload["status"] == "ok", reload
                session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
                sessions.append(session)
                response = client.post(
                    "/api/chat/send",
                    json={
                        "session_id": session,
                        "message": "这是插件连接池重载的技术验收，请简单打个招呼，不把测试内容记成用户事实。",
                    },
                )
                response.raise_for_status()
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                complete = [
                    e["data"] for e in events if e.get("type") == "complete" and e.get("data")
                ]
                plain = [
                    e["data"]
                    for e in events
                    if e.get("type") == "plain"
                    and isinstance(e.get("data"), str)
                    and e.get("chain_type") != "reasoning"
                ]
                text = complete[-1] if complete else "".join(plain)
                assert text and not any(
                    word in text
                    for word in ("PoolClosed", "All chat models failed", "LLM 响应错误")
                ), text
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    rows = conn.execute(
                        "SELECT id,role FROM messages WHERE conversation_id LIKE %s",
                        ("%" + session,),
                    ).fetchall()
                    ids.extend(r["id"] for r in rows)
                    users = [r["id"] for r in rows if r["role"] == "user"]
                    usage = conn.execute(
                        "SELECT count(*) AS n FROM api_usage WHERE message_id=ANY(%s) AND error IS NULL AND input_tokens IS NOT NULL",
                        (users,),
                    ).fetchone()
                    assert usage["n"] == 1, usage
                evidence.append({"reload": index + 1, "chat_succeeded": True, "usage_calls": 1})
            Path("runtime/qa/usage-reload-live.json").write_text(
                json.dumps(evidence, indent=2), "utf-8"
            )
            print(json.dumps({"passed": True, "reloads": evidence}, ensure_ascii=False))
        finally:
            for session in sessions:
                client.delete("/api/v1/chat/sessions/" + session)
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('companion:proactive:owner',0))")
                for session in sessions:
                    rows = conn.execute(
                        "SELECT id FROM messages WHERE conversation_id LIKE %s", ("%" + session,)
                    ).fetchall()
                    ids.extend(r["id"] for r in rows)
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
                        from psycopg import sql

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
    main()
