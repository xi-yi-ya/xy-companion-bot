"""Create the dedicated database using a one-time local administrator session."""

import os

import psycopg
from psycopg import sql

from local_secrets import GLOBAL_DB, save_database

with psycopg.connect(
    host="127.0.0.1",
    port=5432,
    user="codex_agent",
    password=os.environ["CODEX_PG_PASSWORD"],
    dbname="postgres",
    connect_timeout=5,
):
    pass
with psycopg.connect(
    host="127.0.0.1",
    port=5432,
    user="postgres",
    password=os.environ["CODEX_PG_ADMIN_PASSWORD"],
    dbname="postgres",
    connect_timeout=5,
    autocommit=True,
) as conn:
    if not conn.execute(
        "SELECT 1 FROM pg_database WHERE datname=%s", ("companion_bot",)
    ).fetchone():
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier("companion_bot"), sql.Identifier("codex_agent")
            )
        )
save_database("127.0.0.1", 5432, "codex_agent", os.environ["CODEX_PG_PASSWORD"])
print(f"Database ready; encrypted credentials saved to {GLOBAL_DB}")
