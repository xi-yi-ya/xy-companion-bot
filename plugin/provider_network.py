"""Route the official DeepSeek host directly without changing other app traffic."""

import os

import httpx
from httpx._utils import URLPattern


def direct_environment():
    for key in ("NO_PROXY", "no_proxy"):
        previous = os.environ.get(key, "")
        entries = [x.strip() for x in previous.split(",") if x.strip()]
        if "api.deepseek.com" not in entries:
            entries.append("api.deepseek.com")
        os.environ[key] = ",".join(entries)


def ensure_direct(provider):
    """Add one owned host mount to HTTPX 0.28; SDK and client retain ownership.

    Existing in-flight requests keep their transport; no client/pool is closed.
    AsyncClient.aclose() also closes this mount on actual provider shutdown.
    """
    sdk = getattr(provider, "client", None)
    client = getattr(sdk, "_client", None)
    base = getattr(sdk, "base_url", None)
    if (
        not isinstance(client, httpx.AsyncClient)
        or getattr(base, "host", None) != "api.deepseek.com"
        or getattr(base, "scheme", None) != "https"
    ):
        return False
    if vars(client).get("_companion_deepseek_direct"):
        return True
    if not isinstance(getattr(client, "_mounts", None), dict):
        raise RuntimeError("当前 HTTPX 不支持按域名直连，需要核查版本")
    direct = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    mounts = {**client._mounts, URLPattern("all://api.deepseek.com"): direct}
    client._mounts = dict(sorted(mounts.items()))
    client._companion_deepseek_direct = True
    return True
