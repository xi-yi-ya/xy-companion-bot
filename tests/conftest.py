import asyncio
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from local_secrets import database_parameters
from plugin.memory import MemoryStore

# Keep pytest's own scratch data inside this checkout, rather than reusing a
# system temp directory that may belong to a different Windows execution user.
_test_temp = Path(__file__).resolve().parents[1] / "runtime/pytest-temp"
_test_temp.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_test_temp))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest_asyncio.fixture
async def store():
    params = database_parameters()
    schema = "test_companion_" + uuid.uuid4().hex
    with psycopg.connect(**params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    params["options"] = f"-csearch_path={schema}"
    memory = MemoryStore(make_conninfo(**params))
    try:
        await memory.open()
        yield memory
    finally:
        await memory.close()
        with psycopg.connect(**database_parameters(), autocommit=True) as admin:
            assert schema.startswith("test_companion_")
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
