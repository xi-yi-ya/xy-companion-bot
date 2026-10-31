"""Print safe installation and database status without credential values."""

import psycopg

from local_secrets import GLOBAL_DB, database_parameters

with psycopg.connect(**database_parameters()) as conn:
    print("Database:", conn.execute("select current_user,current_database()").fetchone())
    print(
        "Vector extension available:",
        bool(conn.execute("select 1 from pg_available_extensions where name='vector'").fetchone()),
    )
print("Global credential:", GLOBAL_DB)
