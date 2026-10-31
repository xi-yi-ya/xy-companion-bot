"""Exercise real authenticated AstrBot WebChat and memory APIs without printing secrets."""

import argparse
import json
import sys
from pathlib import Path

import httpx
import psycopg

from local_secrets import database_parameters

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--consolidate", action="store_true")
parser.add_argument(
    "--message",
    default="你好，这是本地聊天链路验收。简单打个招呼即可，不要把这条测试消息记成用户事实。",
)
args = parser.parse_args()
with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=120) as client:
    state = client.get("/api/v1/plugins/extensions/astrbot_plugin_companion_memory/state").json()
    assert "people" in state
    print("Memory API connected; retrieval:", state["retrieval"])
    new = client.get("/api/v1/chat/sessions/new").json()
    print("New chat session:", json.dumps(new, ensure_ascii=False))
    session = new["data"]["session_id"]
    response = client.post("/api/chat/send", json={"session_id": session, "message": args.message})
    print("Chat HTTP:", response.status_code)
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    complete = [event for event in events if event.get("type") == "complete"]
    assert complete and complete[-1].get("data"), "No final chat response"
    print("Final reply:", complete[-1]["data"])
    with psycopg.connect(**database_parameters()) as conn:
        roles = conn.execute(
            "SELECT role,count(*) FROM messages WHERE conversation_id LIKE %s GROUP BY role",
            ("%" + session,),
        ).fetchall()
        assert dict(roles).get("user") == 1 and dict(roles).get("assistant") == 1, roles
        print("PostgreSQL transcript roles:", roles)
    state = client.get("/api/v1/plugins/extensions/astrbot_plugin_companion_memory/state").json()
    (ROOT / "runtime/web-smoke-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), "utf-8"
    )
    print("Post-chat memory context runs:", len(state["runs"]))
