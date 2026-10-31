"""Real WebChat + streamed API metering smoke; remove only its own test session."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent
BASE = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"


def main():
    evidence = {}
    session = None
    with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=180) as client:
        started = datetime.now(UTC)
        try:
            session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
            evidence["session"] = session
            prompts = [
                "这是一次公开技术验收，不是用户经历或偏好，请不要形成记忆或申请改人设。"
                "请使用 companion_read_web 读取 https://api-docs.deepseek.com/quick_start/pricing/，"
                "只告诉我 deepseek-flash 的上下文上限并附来源。",
                "继续刚才的技术验收：请复述我刚才让你核对的模型名，并说出你的默认名字。"
                "不要上网，不要记录为用户事实。",
            ]
            evidence["replies"] = []
            for prompt in prompts:
                response = client.post(
                    "/api/chat/send", json={"session_id": session, "message": prompt}
                )
                response.raise_for_status()
                events = [
                    json.loads(s[6:]) for s in response.text.splitlines() if s.startswith("data: ")
                ]
                complete = [e for e in events if e.get("type") == "complete" and e.get("data")]
                assert complete, "WebChat did not complete"
                evidence["replies"].append(complete[-1]["data"])
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                messages = conn.execute(
                    "SELECT id,role FROM messages WHERE conversation_id LIKE %s ORDER BY id",
                    ("%" + session,),
                ).fetchall()
                ids = [r["id"] for r in messages]
                assert [r["role"] for r in messages] == ["user", "assistant", "user", "assistant"]
                usage = conn.execute(
                    "SELECT category,model,input_tokens,output_tokens,cache_hit_tokens,cache_miss_tokens,"
                    "latency_ms,first_token_ms,finish_reason,error,estimated_usd_low,estimated_usd_high "
                    "FROM api_usage WHERE message_id=ANY(%s) OR (started_at>=%s AND category='native_other') "
                    "ORDER BY started_at",
                    (ids, started),
                ).fetchall()
                assert len(usage) >= 3 and all(r["input_tokens"] is not None for r in usage), usage
                assert all(r["model"].startswith("deepseek") and not r["error"] for r in usage)
                assert any(r["first_token_ms"] is not None for r in usage), "No streamed metering"
                web = conn.execute(
                    "SELECT action,success,result_count FROM web_requests WHERE message_id=ANY(%s)",
                    (ids,),
                ).fetchall()
                assert any(r["action"] == "fetch" and r["success"] for r in web)
                contexts = conn.execute(
                    "SELECT details FROM context_runs WHERE message_id=ANY(%s) ORDER BY id", (ids,)
                ).fetchall()
                evidence.update(usage=usage, web=web, contexts=contexts)
                assert contexts[-1]["details"]["history_raw_tokens"] > 0
            metrics = client.get(BASE + "metrics").json()
            assert metrics["count_method"] == "official_deepseek_v4"
            assert metrics["monthly"] and metrics["policy"]["history_max"] == 240000
            assert metrics["web_fees_monthly"]
            evidence["metrics_verified"] = True
        finally:
            if session:
                response = client.delete("/api/v1/chat/sessions/" + session)
                response.raise_for_status()
                with psycopg.connect(**database_parameters()) as conn:
                    ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT id FROM messages WHERE conversation_id LIKE %s",
                            ("%" + session,),
                        ).fetchall()
                    ]
                    for table in ("context_runs",):
                        conn.execute(f"DELETE FROM {table} WHERE message_id=ANY(%s)", (ids,))
                    conn.execute("DELETE FROM messages WHERE id=ANY(%s)", (ids,))
                evidence["test_chat_removed"] = True
                (ROOT / "runtime/qa/native-budget-evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
                )
    print(json.dumps({"metered_calls": len(evidence["usage"]), "test_chat_removed": True}))


if __name__ == "__main__":
    main()
