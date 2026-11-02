"""Install the pinned PG18 Windows extension and verify it in the dedicated database."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(
    description="安装固定版本的 Windows PostgreSQL 18 pgvector。新部署请使用 --files-only，再用管理员 psql 启用扩展。"
)
parser.add_argument(
    "--pg-root", type=Path, required=True, help="PostgreSQL 18 安装目录，不是 data 目录"
)
parser.add_argument(
    "--package", type=Path, default=ROOT / "runtime/downloads/vector.v0.8.7-pg18.zip"
)
parser.add_argument("--files-only", action="store_true")
parser.add_argument("--enable-only", action="store_true")
parser.add_argument(
    "--check-only",
    action="store_true",
    help="只验证 PG18 路径和压缩包 SHA256，不安装、不连接数据库",
)
args = parser.parse_args()
PACKAGE, PGROOT = args.package.resolve(), args.pg_root.resolve()
if not (PGROOT / "bin/pg_config.exe").is_file():
    parser.error("--pg-root 需要是 PostgreSQL 安装目录（包含 bin/pg_config.exe）")
pg_version = subprocess.check_output([str(PGROOT / "bin/pg_config.exe"), "--version"], text=True)
if not pg_version.startswith("PostgreSQL 18."):
    parser.error("这个固定二进制包仅支持 PostgreSQL 18；其他版本请按 pgvector 官方文档安装")
if not PACKAGE.exists():
    PACKAGE.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(
            "https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.7/vector.v0.8.7-pg18.zip"
        )
        response.raise_for_status()
        PACKAGE.write_bytes(response.content)
expected = "7c4ae0e2b2cb1e547779209a34a7d3f05b82fd7912818bf3d7042266eda87e7d"
assert hashlib.sha256(PACKAGE.read_bytes()).hexdigest() == expected, "Package checksum mismatch"
if args.check_only:
    print("PostgreSQL 18 and pinned pgvector archive verified; no files or database changed.")
    sys.exit(0)
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
        if not args.enable_only:
            target.write_bytes(data)
        installed.append(name)
if args.files_only:
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
