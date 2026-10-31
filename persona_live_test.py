"""Verify real DeepSeek requests and owner-approval UI with synthetic L1 data."""

import asyncio
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from local_secrets import database_parameters
from plugin.memory import MemoryStore

ROOT = Path(__file__).resolve().parent
API = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"
sys.stdout.reconfigure(encoding="utf-8")


async def chat(client, session, message):
    response = await client.post("/api/chat/send", json={"session_id": session, "message": message})
    response.raise_for_status()
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    complete = [e["data"] for e in events if e.get("type") == "complete"]
    assert complete, "No final chat reply"
    return complete[-1]


async def main():
    identity = "persona-e2e-" + uuid.uuid4().hex
    person = "validation:" + identity
    store = MemoryStore(make_conninfo(**database_parameters()))
    await store.open()
    owner_request = None
    requested_name = "星河验收" + uuid.uuid4().hex[:6]
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:6185", trust_env=False, timeout=150
        ) as client:
            before = (await client.get(API + "persona")).json()
            session = (await client.get("/api/v1/chat/sessions/new")).json()["data"]["session_id"]
            reply = await chat(
                client,
                session,
                f"这是人设申请功能的技术验收，请调用 companion_propose_persona，仅申请将 L0 的 name 改为“{requested_name}”。"
                "理由是验证手动审批机制。请引用本条原话作为来源，先提交具体申请，等待我在设置页手动批准。"
                "现在不要改动自己的设定，也不要把验收当成用户长期事实。",
            )
            after = (await client.get(API + "persona", params={"person": "webchat:owner"})).json()
            assert (after["profile"], after["version"]) == (before["profile"], before["version"])
            proposals = [
                p
                for p in after["proposals"]
                if p["new_value"] == requested_name and p["status"] == "pending"
            ]
            assert proposals, f"No pending real tool request; reply={reply}"
            owner_request = proposals[0]["id"]
            print(
                "Real chat -> companion_propose_persona -> checked pending request; L0 unchanged: PASS",
                flush=True,
            )
            second = await chat(
                client, session, "我在聊天里说“同意”了。请只说你目前已生效的名字，不要提交新申请。"
            )
            after_chat = (
                await client.get(API + "persona", params={"person": "webchat:owner"})
            ).json()
            assert after_chat["profile"] == before["profile"]
            assert before["profile"]["name"] in second, second
            assert any(
                p["id"] == owner_request and p["status"] == "pending"
                for p in after_chat["proposals"]
            )
            print(
                "Chat-only consent cannot apply the request; old persona remains active: PASS",
                flush=True,
            )
            for i in range(6):
                await store.record(
                    "validation",
                    identity,
                    identity,
                    f"source-{i}",
                    "user",
                    f"第{i + 1}次聊这个偏好：我希望你在我倾诉时先听我说完，简短回应感受，再问我要不要建议。"
                    "这是我这几天反复确认、以后也希望保持的互动习惯。",
                    datetime.now(UTC) - timedelta(days=1 + i // 2),
                )
            result = (await client.post(API + "persona/discover", json={"person": person})).json()
            assert result.get("status") == "complete" and result.get("candidates", 0) > 0, result
            state = (await client.get(API + "persona", params={"person": person})).json()
            pending = [p for p in state["proposals"] if p["status"] == "pending"]
            assert pending and not state["traits"], state["proposals"]
            print(
                "Real cross-day discovery + consistency check -> pending L1; no automatic trait: PASS",
                flush=True,
            )
            (ROOT / "runtime/qa/persona-live-input.json").write_text(
                json.dumps(
                    {
                        "person": person,
                        "l1_proposal": pending[0]["id"],
                        "owner_request": owner_request,
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
            outcome = await asyncio.to_thread(
                subprocess.run, ["node", "persona_browser_test.cjs"], cwd=ROOT, check=False
            )
            assert outcome.returncode == 0, "Browser approval test failed"
            final = (await client.get(API + "persona", params={"person": person})).json()
            assert len([t for t in final["traits"] if t["active"] and not t["needs_review"]]) == 1
            assert (final["profile"], final["version"]) == (before["profile"], before["version"])
            (ROOT / "runtime/qa/persona-live-evidence.json").write_text(
                json.dumps(
                    {
                        "bot_request": owner_request,
                        "initial_reply": reply,
                        "chat_consent_reply": second,
                        "discovery": result,
                        "synthetic_traits": final["traits"],
                        "global_profile_unchanged": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "utf-8",
            )
            print(
                "Owner UI approval persisted L1 while global L0 remained unchanged: PASS",
                flush=True,
            )
    finally:
        if owner_request:
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:6185", trust_env=False
            ) as client:
                state = (
                    await client.get(API + "persona", params={"person": "webchat:owner"})
                ).json()
                if any(
                    p["id"] == owner_request and p["status"] == "pending"
                    for p in state["proposals"]
                ):
                    await client.post(
                        API + "persona/decide",
                        json={"person": "webchat:owner", "id": owner_request, "action": "reject"},
                    )
        await store.close()
        assert person.startswith("validation:persona-e2e-")
        with psycopg.connect(**database_parameters()) as conn:
            for table in (
                "web_requests",
                "persona_events",
                "persona_traits",
                "persona_proposals",
                "persona_jobs",
                "context_runs",
                "daily_jobs",
            ):
                # Identifiers are a fixed local allowlist; the person remains parameterized.
                conn.execute(f"DELETE FROM {table} WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM messages WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM platform_accounts WHERE person_id=%s", (person,))
            conn.execute("DELETE FROM persons WHERE id=%s", (person,))
        print("Synthetic persona data cleaned; owner profile preserved.", flush=True)


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
