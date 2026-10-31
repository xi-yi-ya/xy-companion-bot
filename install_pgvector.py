"""Install the pinned PG18 Windows extension and verify it in the dedicated database."""

import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "runtime/downloads/vector.v0.8.7-pg18.zip"
PGROOT = Path(r"D:\Flie\PostgreSQL\18")
expected = "7c4ae0e2b2cb1e547779209a34a7d3f05b82fd7912818bf3d7042266eda87e7d"
assert hashlib.sha256(PACKAGE.read_bytes()).hexdigest() == expected, "Package checksum mismatch"
installed = []
with zipfile.ZipFile(PACKAGE) as archive:
    for member in archive.infolist():
        if member.is_dir():
            continue
        name = member.filename
        if name != "lib/vector.dll" and not (
            name.startswith("share/extension/vector")
            and (name.endswith(".sql") or name.endswith(".control"))
        ):
            continue
        target = (PGROOT / name).resolve()
        if not target.is_relative_to(PGROOT.resolve()):
            raise ValueError("Unsafe archive path")
        data = archive.read(member)
        if target.exists() and target.read_bytes() != data:
            backup = ROOT / "runtime" / "pgvector-backup" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        if "--enable-only" not in sys.argv:
            target.write_bytes(data)
        installed.append(name)
if "--files-only" in sys.argv:
    (ROOT / "runtime/pgvector-files-ready").write_text("ok", "utf-8")
    sys.exit(0)
with psycopg.connect(
    host="127.0.0.1",
    port=5432,
    user="postgres",
    password=os.environ["CODEX_PG_ADMIN_PASSWORD"],
    dbname="companion_bot",
) as conn:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[
        0
    ]
    distance = conn.execute("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector").fetchone()[0]
manifest = {
    "version": version,
    "postgres_major": 18,
    "sha256": expected,
    "files": installed,
    "source": "https://github.com/andreiramani/pgvector_pgsql_windows/releases/tag/0.8.7",
    "smoke_cosine_distance": distance,
}
(ROOT / "runtime" / "pgvector-install.json").write_text(json.dumps(manifest, indent=2), "utf-8")
print(f"pgvector {version} enabled in companion_bot; identical-vector cosine distance = {distance}")
