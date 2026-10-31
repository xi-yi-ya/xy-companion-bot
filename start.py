"""Prepare and run a pinned local AstrBot instance without copying the API key."""

import asyncio
import json
import os
import re
import runpy
import secrets
import shutil
import sys
from pathlib import Path

from psycopg.conninfo import make_conninfo

from local_instance import InstanceGuard, instance_id
from local_secrets import database_parameters

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
SOURCE = ROOT / "vendor" / "AstrBot"


def prepare():
    settings_path = ROOT / "local_config.json"
    if not settings_path.exists():
        shutil.copyfile(ROOT / "local_config.json.example", settings_path)
    settings = json.loads(settings_path.read_text("utf-8"))
    text = Path(settings["deepseek_key_file"]).read_text("utf-8-sig").strip()
    match = re.search(r"sk-[A-Za-z0-9_-]{12,}", text)
    if not match:
        raise ValueError("No API key found in the configured credential file")
    os.environ["DEEPSEEK_API_KEY"] = match.group()
    os.environ["COMPANION_DATABASE_DSN"] = make_conninfo(**database_parameters())
    os.environ["COMPANION_PROVIDER_ID"] = settings["provider_id"]
    os.environ["COMPANION_EMBEDDING_DIR"] = str(RUNTIME / "models/bge-small-zh-v1.5")
    os.environ["ASTRBOT_ROOT"] = str(RUNTIME)
    os.environ["COMPANION_INSTANCE_ID"] = instance_id(ROOT)
    sys.path.insert(0, str(SOURCE))
    (RUNTIME / "data" / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT / "plugin",
        RUNTIME / "data" / "plugins" / "astrbot_plugin_companion_memory",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    config_file = RUNTIME / "data" / "cmd_config.json"
    if not config_file.exists():
        import copy

        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.utils.auth_password import (
            hash_dashboard_password,
            hash_md5_dashboard_password,
        )

        config = copy.deepcopy(DEFAULT_CONFIG)
        password = secrets.token_urlsafe(18) + "Aa1"
        config["dashboard"].update(
            host="127.0.0.1",
            port=settings["port"],
            username="owner",
            password=hash_md5_dashboard_password(password),
            pbkdf2_password=hash_dashboard_password(password),
            password_storage_upgraded=True,
            password_change_required=False,
        )
        config["provider"] = [
            {
                "id": settings["provider_id"],
                "type": "openai_chat_completion",
                "provider": "deepseek",
                "provider_type": "chat_completion",
                "enable": True,
                "key": ["$DEEPSEEK_API_KEY"],
                "api_base": "https://api.deepseek.com/v1",
                "model": settings["model"],
                "timeout": 90,
                "proxy": "",
                "custom_extra_body": {
                    "thinking": {"type": "disabled"},
                    "max_tokens": 4096,
                },
            }
        ]
        config["agent_runner"]["config"]["model"].update(
            provider_id=settings["provider_id"], request_max_retries=2
        )
        config["agent_runner"]["config"]["compression"].update(
            overflow_strategy="truncate_by_turns", max_turns=20
        )
        config["agent_runner"]["config"]["misc"]["max_steps"] = 5
        config["provider_settings"].update(datetime_system_prompt=False, streaming_response=False)
        config["provider_settings"]["proactive_capability"]["add_cron_tools"] = False
        config["admins_id"] = ["owner"]
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    return settings


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr:
        sys.stderr.reconfigure(encoding="utf-8")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if "--prepare-only" in sys.argv:
        prepare()
        print("Runtime prepared; API key referenced through environment only")
    else:
        if "--run" not in sys.argv:
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "--run"])
        guard = InstanceGuard(ROOT)
        if not guard.acquire(wait_seconds=2):
            print("Companion Bot is already running; no duplicate backend was started.")
            raise SystemExit(0)
        prepare()
        os.chdir(RUNTIME)
        (RUNTIME / "server.pid").write_text(str(os.getpid()), "utf-8")
        from local_web import install_local_web

        install_local_web(int(json.loads((ROOT / "local_config.json").read_text("utf-8"))["port"]))
        sys.argv = [str(ROOT / "start.py")]
        try:
            runpy.run_path(str(SOURCE / "main.py"), run_name="__main__")
        finally:
            guard.close()
