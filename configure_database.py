"""Verify an existing dedicated database and save current-user DPAPI credentials."""

import argparse
import getpass
import json
import sys
from pathlib import Path

import psycopg

from local_secrets import save_database

ROOT = Path(__file__).resolve().parent


def configure(host, port, user, password, root=ROOT):
    root = Path(root)
    if not 1 <= port <= 65535 or not user.strip() or not password:
        raise ValueError("端口、数据库用户名或密码无效")
    # Never create/alter roles or databases here, or store an unverified login.
    with psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname="companion_bot",
        connect_timeout=5,
    ) as conn:
        vector = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        if not vector:
            raise ValueError("companion_bot 尚未启用 pgvector；请先按部署文档创建扩展")
        allowed = conn.execute(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ).fetchone()
        if not allowed[0]:
            raise ValueError("数据库账号需要在 companion_bot.public 中创建表的权限")
    config_file = root / "local_config.json"
    settings = json.loads(
        (config_file if config_file.exists() else root / "local_config.json.example").read_text(
            "utf-8-sig"
        )
    )
    destination = root / "runtime/secrets/postgresql.json"
    save_database(host, port, user, password, path=destination)
    settings["database_credentials_file"] = "runtime/secrets/postgresql.json"
    config_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    return destination, vector[0]


def main():
    parser = argparse.ArgumentParser(
        description="连接已创建的 companion_bot 数据库；密码隐藏输入并以 Windows DPAPI 加密保存。"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="companion_bot")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("当前安装器使用 Windows 当前用户 DPAPI；仅支持 Windows。")
    try:
        target, version = configure(
            args.host, args.port, args.user, getpass.getpass("数据库账号密码（不显示）：")
        )
    except (ValueError, psycopg.Error) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise SystemExit("数据库配置未保存：" + message) from None
    print(f"companion_bot 连接成功，pgvector {version}；凭据已加密保存到 {target}")


if __name__ == "__main__":
    main()
