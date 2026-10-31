"""Verify the real chat model searches and reads official sources through tools."""

import json
import sys
from pathlib import Path

import httpx
import psycopg

from local_secrets import database_parameters

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"
with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=150) as client:
    settings = client.get(API + "web").json()["settings"]
    assert settings["enabled"], "Enable web access in settings before this optional live test"
    session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
    response = client.post(
        "/api/chat/send",
        json={
            "session_id": session,
            "message": "听说 DeepSeek 现在提供独立的搜索和网页读取接口了，你能查一下吗？"
            "请先搜索官方资料，再读取 https://api-docs.deepseek.com/guides/search/ 的正文，"
            "告诉我这两个接口的路径和结果格式，简单两句并附来源链接。"
            "这是一条联网功能技术验收，不要记录为用户长期事实。",
        },
    )
    response.raise_for_status()
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    complete = [e["data"] for e in events if e.get("type") == "complete"]
    assert complete, "No final reply"
    final = complete[-1]
    with psycopg.connect(**database_parameters()) as conn:
        requests = conn.execute(
            "SELECT w.action,w.success,w.result_count FROM web_requests w JOIN messages m ON m.id=w.message_id WHERE m.conversation_id LIKE %s ORDER BY w.id",
            ("%" + session,),
        ).fetchall()
    assert any(action == "search" and success and count for action, success, count in requests), (
        requests
    )
    assert any(action == "fetch" and success for action, success, _ in requests), requests
    assert "/v1/tools/search" in final and "/v1/tools/fetch" in final, final
    assert "https://api-docs.deepseek.com/" in final, final
    assert len(requests) <= settings["max_calls"]
    (ROOT / "runtime/qa/web-live-evidence.json").write_text(
        json.dumps(
            {"session": session, "requests": requests, "reply": final}, ensure_ascii=False, indent=2
        ),
        "utf-8",
    )
    print("Real DeepSeek chat -> web search -> official page fetch -> cited answer: PASS")
    print("Web request audit:", requests)
