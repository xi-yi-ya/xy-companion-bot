"""Check DeepSeek access without logging credentials or making a generation call."""

import json
import re
from pathlib import Path

import httpx

root = Path(__file__).resolve().parent
settings_path = root / "local_config.json"
settings = json.loads(
    (settings_path if settings_path.exists() else root / "local_config.json.example").read_text(
        "utf-8"
    )
)
key = re.search(
    r"sk-[A-Za-z0-9_-]{12,}", Path(settings["deepseek_key_file"]).read_text("utf-8-sig")
).group()
with httpx.Client(timeout=30, proxy="http://127.0.0.1:7890") as client:
    response = client.get(
        "https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"}
    )
    print("DeepSeek model endpoint HTTP:", response.status_code)
    if response.is_success:
        print("Available models:", [m["id"] for m in response.json().get("data", [])])
