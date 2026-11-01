"""Real Web uploads/model/continuity test; removes its own technical chat only."""

import json
import random
import re
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from PIL import Image, ImageDraw, ImageFont
from psycopg.rows import dict_row

from local_secrets import database_parameters
from plugin.proactive import next_allowed

ROOT = Path(__file__).resolve().parent
BASE = "/api/v1/plugins/extensions/astrbot_plugin_companion_memory/"


def main():
    folder = ROOT / "runtime/qa"
    document, image = folder / "file-acceptance.docx", folder / "image-acceptance.png"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="word"><w:body><w:p><w:r><w:t>This is a technical test, not a user fact. Document code: ORBIT-7319.</w:t></w:r></w:p></w:body></w:document>',
        )
    picture = Image.new("RGB", (900, 450), "#e23edf")
    ImageDraw.Draw(picture).text(
        (70, 150),
        "BIRD-5821",
        font=ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 92),
        fill="black",
    )
    picture.save(image)
    with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
        original_state = conn.execute(
            "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
        ).fetchone()
    evidence, session, ids = {"started_at": datetime.now(UTC).isoformat()}, None, []
    with httpx.Client(base_url="http://127.0.0.1:6185", timeout=280, trust_env=False) as client:

        def send(message):
            response = client.post(
                "/api/chat/send", json={"session_id": session, "message": message}
            )
            response.raise_for_status()
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            finished = [e["data"] for e in events if e.get("type") == "complete" and e.get("data")]
            assert any(e.get("type") in ("complete", "end") for e in events), (
                "Native Web chat did not complete"
            )
            if finished:
                return finished[-1]
            # Receipt-sensitive turns intentionally use the native non-stream
            # path: visible plain frames precede an empty closing frame.
            plain = [
                e["data"]
                for e in events
                if e.get("type") == "plain"
                and isinstance(e.get("data"), str)
                and e.get("chain_type") != "reasoning"
            ]
            assert plain, "Native Web chat returned no visible reply"
            return "".join(plain)

        def upload(path, mime):
            with path.open("rb") as stream:
                result = client.post(
                    "/api/chat/post_file", files={"file": (path.name, stream, mime)}
                ).json()
            assert result["status"] == "ok", result
            return result["data"]

        try:
            original_persona = client.get(
                BASE + "persona", params={"person": "webchat:owner"}
            ).json()
            session = client.get("/api/v1/chat/sessions/new").json()["data"]["session_id"]
            doc = upload(
                document, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            reply = send(
                [
                    {
                        "type": "plain",
                        "text": "这是技术验收材料，不是我的经历或偏好，不要形成记忆或修改人设。请读取这个 Word 的正文，回答里面的英文代号。",
                    },
                    {
                        "type": "file",
                        "attachment_id": doc["attachment_id"],
                        "filename": doc["filename"],
                    },
                ]
            )
            assert "ORBIT-7319" in reply, "DOCX body did not reach the model"
            evidence["docx_uploaded_and_answered"] = True
            photo = upload(image, "image/png")
            reply = send(
                [
                    {
                        "type": "plain",
                        "text": "继续技术验收，请读出这张图里的英文代号，并说明背景色。这张图也不是用户事实。",
                    },
                    {
                        "type": "image",
                        "attachment_id": photo["attachment_id"],
                        "filename": photo["filename"],
                    },
                ]
            )
            assert "BIRD-5821" in reply, "Image text did not reach the model"
            evidence["image_uploaded_and_read"] = True
            reply = send("不重新上传、不联网，刚才文档里的代号和图片里的代号分别是什么？")
            assert "ORBIT-7319" in reply and "BIRD-5821" in reply, "Attachment continuity failed"
            evidence["follow_up_without_upload"] = True
            reply = send(
                "再做一次技术验收：读取 https://api-docs.deepseek.com/guides/vision/ ，告诉我当前 deepseek-flash 能否看图片并附来源。不要形成用户记忆。"
            )
            assert "deepseek" in reply.lower() and "http" in reply, "Link content missing"
            evidence["link_answered"] = True
            reply = send(
                "这是接口验收，不允许批准或直接修改人设。请实际调用 companion_propose_persona，申请一条 L1 的 interaction_style，内容为“认真听完用户的补充再回应”，理由为“验证申请能否真实入库并等待审批”，quote 使用本消息中的“请实际调用 companion_propose_persona”。提交后报告真实编号和状态。"
            )
            evidence["persona_reply"] = reply
            assert "<tool_call>" not in reply and re.search(r"#\d+", reply), (
                "No verified persona receipt in final answer"
            )
            current_persona = client.get(
                BASE + "persona", params={"person": "webchat:owner"}
            ).json()
            assert all(
                current_persona[k] == original_persona[k] for k in ("profile", "version", "traits")
            )
            with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM messages WHERE conversation_id LIKE %s", ("%" + session,)
                    ).fetchall()
                ]
                assets = conn.execute(
                    "SELECT kind,status,length(context_text) AS chars,error FROM message_attachments WHERE message_id=ANY(%s)",
                    (ids,),
                ).fetchall()
                usage = conn.execute(
                    "SELECT category,model,input_tokens,output_tokens,cache_hit_tokens,error FROM api_usage WHERE message_id=ANY(%s) ORDER BY started_at",
                    (ids,),
                ).fetchall()
                raw = conn.execute(
                    "SELECT content FROM messages WHERE id=ANY(%s) AND role='user'", (ids,)
                ).fetchall()
                proposals = conn.execute(
                    "SELECT id,status FROM persona_proposals WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(sources) s WHERE (s->>'message_id')::bigint=ANY(%s))",
                    (ids,),
                ).fetchall()
                assert proposals and all(p["status"] in ("pending", "blocked") for p in proposals)
                assert any(f"#{p['id']}" in reply for p in proposals)
                evidence.update(native_persona_request_has_receipt=True, persona_unchanged=True)
                assert all(r["status"] == "ready" for r in assets), assets
                assert {r["kind"] for r in assets} >= {"file", "image", "link"}
                assert any(
                    r["category"] == "attachment_vision" and r["input_tokens"] and not r["error"]
                    for r in usage
                )
                assert all(
                    "ORBIT-7319" not in r["content"] and "BIRD-5821" not in r["content"]
                    for r in raw
                )
                evidence.update(assets=assets, usage=usage, raw_user_facts_not_replaced=True)
        finally:
            if session:
                client.delete("/api/v1/chat/sessions/" + session).raise_for_status()
                with psycopg.connect(**database_parameters(), row_factory=dict_row) as conn:
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('companion:proactive:owner',0))"
                    )
                    ids = [
                        r["id"]
                        for r in conn.execute(
                            "SELECT id FROM messages WHERE conversation_id LIKE %s",
                            ("%" + session,),
                        ).fetchall()
                    ]
                    state = conn.execute(
                        "SELECT * FROM proactive_state WHERE person_id='webchat:owner'"
                    ).fetchone()
                    test_proposals = [
                        r["id"]
                        for r in conn.execute(
                            "SELECT id FROM persona_proposals WHERE status IN ('pending','blocked') AND EXISTS (SELECT 1 FROM jsonb_array_elements(sources) s WHERE (s->>'message_id')::bigint=ANY(%s))",
                            (ids,),
                        ).fetchall()
                    ]
                    conn.execute(
                        "DELETE FROM persona_events WHERE proposal_id=ANY(%s)", (test_proposals,)
                    )
                    conn.execute(
                        "DELETE FROM persona_proposals WHERE id=ANY(%s)", (test_proposals,)
                    )
                    conn.execute("DELETE FROM context_runs WHERE message_id=ANY(%s)", (ids,))
                    conn.execute("DELETE FROM messages WHERE id=ANY(%s)", (ids,))
                    # Do not clobber real user messages arriving during QA.
                    if state and state["last_user_id"] in ids:
                        latest = conn.execute(
                            "SELECT * FROM messages WHERE person_id='webchat:owner' AND role='user' ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                        if (
                            original_state
                            and latest
                            and latest["id"] == original_state["last_user_id"]
                        ):
                            columns = [k for k in original_state if k != "person_id"]
                            conn.execute(
                                "UPDATE proactive_state SET "
                                + ",".join(k + "=%s" for k in columns)
                                + " WHERE person_id='webchat:owner'",
                                [original_state[k] for k in columns],
                            )
                        elif latest:
                            settings = client.get(BASE + "proactive").json()["settings"]
                            seconds = random.SystemRandom().randint(
                                round(settings["min_hours"] * 3600),
                                round(settings["max_hours"] * 3600),
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
                            conn.execute(
                                "DELETE FROM proactive_state WHERE person_id='webchat:owner'"
                            )
                evidence["test_chat_removed"] = True
                (folder / "attachments-live-evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2, default=str), "utf-8"
                )
    print(
        json.dumps(
            {k: v for k, v in evidence.items() if k not in ("usage",)},
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
