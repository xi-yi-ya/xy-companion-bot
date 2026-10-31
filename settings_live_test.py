"""Verify that a saved persona changes the next real chat reply, then restore it."""

import json
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")
with httpx.Client(base_url="http://127.0.0.1:6185", trust_env=False, timeout=120) as client:
    endpoint = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/settings"
    original = client.get(endpoint).json()["settings"]
    try:
        test = {
            **original,
            "bot_name": "星河验收伙伴",
            "persona": "你是一个陪伴聊天的伙伴。别人问你的名字时，直接说自己的名字。",
        }
        response = client.post(endpoint, json=test)
        assert response.status_code == 200 and response.json()["saved"]
        session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
        response = client.post(
            "/api/chat/send",
            json={
                "session_id": session,
                "message": "这是一条技术验收消息，不记录为用户事实。请只说出你的名字。",
            },
        )
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        final = [e["data"] for e in events if e.get("type") == "complete"][-1]
        assert "星河验收伙伴" in final, final
        print("Saved persona takes effect in the next live DeepSeek reply: PASS")
    finally:
        restored = client.post(endpoint, json=original)
        assert restored.status_code == 200
        assert client.get(endpoint).json()["settings"] == original
        print("Original persona restored: PASS")
