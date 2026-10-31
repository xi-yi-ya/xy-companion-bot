"""Check persistence, extension status, and credential-safe runtime configuration."""

import json
import re
import sqlite3
from pathlib import Path

import httpx
import psycopg

from local_secrets import GLOBAL_DB, database_parameters

root = Path(__file__).resolve().parent
config = (root / "runtime/data/cmd_config.json").read_text("utf-8")
settings = json.loads((root / "local_config.json").read_text("utf-8"))
key = re.search(
    r"sk-[A-Za-z0-9_-]{12,}", Path(settings["deepseek_key_file"]).read_text("utf-8-sig")
).group()
assert key not in config and "$DEEPSEEK_API_KEY" in config
global_record = json.loads(GLOBAL_DB.read_text("utf-8"))
assert "password_dpapi" in global_record and "password" not in global_record
with psycopg.connect(**database_parameters()) as conn:
    print(
        "pgvector:",
        conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0],
    )
    print(
        "Persistent transcript:",
        conn.execute("SELECT role,count(*) FROM messages GROUP BY role ORDER BY role").fetchall(),
    )
    assert (
        conn.execute("SELECT count(*) FROM persons WHERE id LIKE 'validation:e2e-%'").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM messages WHERE person_id='webchat:owner'").fetchone()[0]
        >= 4
    )
with sqlite3.connect((root / "runtime/data/data_v4.db").as_uri() + "?mode=ro", uri=True) as native:
    histories = native.execute("SELECT content FROM conversations").fetchall()
    assert not any("reference_memories" in str(row[0]) for row in histories), (
        "Temporary injected memories leaked into persistent AstrBot history"
    )
    print("Temporary injection is absent from saved AstrBot history: PASS")
with httpx.Client(trust_env=False, timeout=15) as client:
    assert client.get("http://127.0.0.1:6185/").status_code == 200
    print("Web service after restart: PASS")
print(
    "Credential checks: API key not copied into AstrBot config; global PostgreSQL password protected with DPAPI"
)
