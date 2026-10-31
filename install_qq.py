"""Install a pinned, self-contained NapCat Node runtime for this bot only."""

import hashlib
import json
import secrets
import zipfile
from pathlib import Path

import httpx

VERSION = "v4.18.42"
URL = (
    f"https://github.com/NapNeko/NapCatQQ/releases/download/{VERSION}/NapCat.Shell.Windows.Node.zip"
)
SHA256 = "77397ac38d66f46383e48f08f35d5f9d37ec575c22069f62d6c600e73b4521ed"
ROOT = Path(__file__).resolve().parent


def install(root=ROOT):
    root = Path(root)
    folder = root / "runtime/qq"
    folder.mkdir(parents=True, exist_ok=True)
    archive = root / f"runtime/downloads/NapCat.Shell.Windows.Node-{VERSION}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        with httpx.Client(timeout=180, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", URL) as response:
                response.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != SHA256:
        raise ValueError("NapCat package checksum differs from its official release")
    target = (folder / "napcat").resolve()
    # Re-running setup preserves the installed runtime and QQ session data.
    if not (target / "node.exe").exists():
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                if not (target / item.filename).resolve().is_relative_to(target):
                    raise ValueError("Unexpected path in the NapCat archive")
            bundle.extractall(target)
    settings_path = folder / "settings.json"
    if not settings_path.exists():
        settings = {
            "enabled": True,
            "webui_port": 6099,
            "ws_port": 6199,
            "webui_token": secrets.token_urlsafe(32),
            "ws_token": secrets.token_urlsafe(32),
            "bind_code": secrets.token_hex(6).upper(),
            "owner_qq": "",
        }
        settings_path.write_text(json.dumps(settings, indent=2), "utf-8")
    settings = json.loads(settings_path.read_text("utf-8"))
    # This release migrates its live config into the isolated QQ data root.
    config_dir = folder / "data/NapCat/config"
    config_dir.mkdir(parents=True, exist_ok=True)
    webui = config_dir / "webui.json"
    current = json.loads(webui.read_text("utf-8")) if webui.exists() else {}
    current.update(host="127.0.0.1", port=settings["webui_port"], token=settings["webui_token"])
    webui.write_text(json.dumps(current, indent=2), "utf-8")
    onebot = {
        "network": {
            "websocketClients": [
                {
                    "name": "companion-local",
                    "enable": True,
                    "url": f"ws://127.0.0.1:{settings['ws_port']}/ws",
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": settings["ws_token"],
                    "reconnectInterval": 5000,
                    "heartInterval": 30000,
                    "debug": False,
                }
            ]
        }
    }
    (config_dir / "onebot11.json").write_text(json.dumps(onebot, indent=2), "utf-8")
    (folder / "installation.json").write_text(
        json.dumps({"version": VERSION, "url": URL, "sha256": SHA256}, indent=2), "utf-8"
    )
    print("QQ runtime ready; official release checksum verified; desktop QQ is unchanged.")


if __name__ == "__main__":
    install()
