"""Windows current-user DPAPI storage for local PostgreSQL credentials."""

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

GLOBAL_DB = Path.home() / ".codex" / "credentials" / "postgresql-local.json"
ROOT = Path(__file__).resolve().parent


def credential_path():
    """Project-local credentials for new installs; preserve existing global setup."""
    configured = os.environ.get("COMPANION_DB_CREDENTIALS")
    config = ROOT / "local_config.json"
    if not configured and config.is_file():
        configured = json.loads(config.read_text("utf-8-sig")).get("database_credentials_file")
    if not configured:
        return GLOBAL_DB
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


def dpapi(value, decrypt=False):
    buf = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    dll = ctypes.windll.crypt32
    if decrypt:
        ok = dll.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        )
    else:
        ok = dll.CryptProtectData(
            ctypes.byref(source),
            "Codex PostgreSQL",
            None,
            None,
            None,
            1,
            ctypes.byref(target),
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def save_database(host, port, user, password, *, path=None):
    destination = Path(path) if path is not None else credential_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "host": host,
        "port": port,
        "user": user,
        "password_dpapi": base64.b64encode(dpapi(password.encode())).decode(),
        "protection": "Windows DPAPI current user",
        "default_database": "companion_bot",
    }
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")


def database_parameters(database="companion_bot"):
    record = json.loads(credential_path().read_text("utf-8"))
    return {
        "host": record["host"],
        "port": record["port"],
        "user": record["user"],
        "password": dpapi(base64.b64decode(record["password_dpapi"]), True).decode(),
        "dbname": database,
        "connect_timeout": 5,
    }
