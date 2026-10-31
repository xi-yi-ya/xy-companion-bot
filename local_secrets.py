"""Windows current-user DPAPI storage for local PostgreSQL credentials."""

import base64
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

GLOBAL_DB = Path.home() / ".codex" / "credentials" / "postgresql-local.json"


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


def save_database(host, port, user, password):
    GLOBAL_DB.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "host": host,
        "port": port,
        "user": user,
        "password_dpapi": base64.b64encode(dpapi(password.encode())).decode(),
        "protection": "Windows DPAPI current user",
        "default_database": "companion_bot",
    }
    GLOBAL_DB.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")


def database_parameters(database="companion_bot"):
    record = json.loads(GLOBAL_DB.read_text("utf-8"))
    return {
        "host": record["host"],
        "port": record["port"],
        "user": record["user"],
        "password": dpapi(base64.b64decode(record["password_dpapi"]), True).decode(),
        "dbname": database,
        "connect_timeout": 5,
    }
