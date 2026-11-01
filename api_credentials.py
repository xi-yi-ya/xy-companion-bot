"""DeepSeek credential loading without exposing secrets in configuration or logs."""

import os
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")


def read_key_file(path):
    path = Path(path)
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        text = "".join(ElementTree.fromstring(xml).itertext())
    else:
        text = path.read_text("utf-8-sig")
    values = list(dict.fromkeys(KEY_PATTERN.findall(text)))
    if len(values) != 1:
        raise ValueError("凭据文件需要包含一枚可识别的 DeepSeek Key")
    return values[0]


def user_environment():
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, "DEEPSEEK_API_KEY")[0]
    except FileNotFoundError:
        return ""


def load_deepseek_key(settings):
    # A running tray process can inherit an older environment. Read the current
    # user's persisted setting first so changing the key does not need a logout.
    value = user_environment() or os.environ.get("DEEPSEEK_API_KEY", "")
    if value:
        if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value.strip()):
            raise ValueError("DEEPSEEK_API_KEY 环境变量格式无效")
        return value.strip()
    source = settings.get("deepseek_key_file")
    if source and Path(source).is_file():
        return read_key_file(source)
    raise ValueError(
        "未找到 DeepSeek 凭据：请设置用户环境变量 DEEPSEEK_API_KEY，或提供有效的 Key 文件"
    )


def save_user_key(value):
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value.strip()):
        raise ValueError("DeepSeek Key 格式无效")
    import ctypes
    import winreg

    value = value.strip()
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "DEEPSEEK_API_KEY", 0, winreg.REG_SZ, value)
    os.environ["DEEPSEEK_API_KEY"] = value
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 1000, None)
