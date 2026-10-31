"""Remove only empty schemas left by this project's failed fixture startup."""

import re

import psycopg
from psycopg import sql

from local_secrets import database_parameters

with psycopg.connect(**database_parameters(), autocommit=True) as conn:
    schemas = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test_companion_%'"
    ).fetchall()
    count = 0
    for (name,) in schemas:
        if not re.fullmatch("test_companion_[0-9a-f]{32}", name):
            continue
        if not conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=%s", (name,)).fetchone():
            conn.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(name)))
            count += 1
print("Removed empty test schemas:", count)
