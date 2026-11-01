"""Qzone read-only HTTP adapter. No publish, like, comment or delete routes."""

import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from .qzone_read.parser import cookie_gtk, extract_feed_page, parse_index_html
from .qzone_read.social import extract_comments, extract_images
from .qzone_read.utils import extract_callback_json

INDEX = "https://h5.qzone.qq.com/mqzone/index"
ACTIVE = "https://h5.qzone.qq.com/webapp/json/mqzone_feeds/getActiveFeeds"
LEGACY = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
COOKIE_KEYS = {"uin", "p_uin", "skey", "p_skey", "pt4_token", "ptcz", "RK"}


class QzoneReadError(ValueError):
    pass


def parse_cookies(text):
    result = {}
    for part in str(text or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key in COOKIE_KEYS and not any(c in value for c in "\r\n"):
            result[key] = value.strip()
    return result


def trusted_image(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in ("http", "https")
        and not parsed.username
        and not parsed.password
        and (parsed.port in (None, 80, 443))
        and any(
            host == d or host.endswith("." + d)
            for d in ("qpic.cn", "qlogo.cn", "gtimg.cn", "photo.qq.com", "qzone.qq.com")
        )
    )


def decode_payload(text):
    try:
        return json.loads(text)
    except ValueError:
        data = extract_callback_json(text)
        if data is None:
            raise QzoneReadError("空间返回的内容无法解析，可能需要重新登录") from None
        return data


def normalize_posts(payload, account):
    _, entries = extract_feed_page(payload)
    result = []
    seen = set()
    for entry in entries[:40]:
        if not entry.hostuin or not entry.fid or (entry.hostuin, entry.fid) in seen:
            continue
        seen.add((entry.hostuin, entry.fid))
        # Restrict source IDs used in links; HTML and request tokens stay out of PG.
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", entry.fid):
            continue
        images = [
            url
            for url in extract_images(entry.raw, fid=entry.fid, hostuin=entry.hostuin)
            if trusted_image(url)
        ][:9]
        comments = extract_comments(entry.raw)[:5]
        body = entry.summary[:16000]
        identity = f"{account}:{entry.hostuin}:{entry.appid}:{entry.fid}"
        result.append(
            {
                "key": hashlib.sha256(identity.encode()).hexdigest(),
                "account": str(account),
                "author": str(entry.hostuin),
                "nickname": entry.nickname[:100],
                "fid": entry.fid,
                "appid": entry.appid,
                "posted_at": max(0, entry.created_at),
                "body": body,
                "images": images,
                "comments": [
                    {"nickname": c.nickname, "content": c.content[:1200]} for c in comments
                ],
                "url": f"https://user.qzone.qq.com/{entry.hostuin}/mood/{entry.fid}",
            }
        )
    return result[:20]


class QzoneReader:
    def __init__(self, credentials, *, transport=None):
        self.credentials, self.transport = credentials, transport
        self.trace = []

    async def fetch(self):
        account, cookies = await self.credentials()
        identity = str(cookies.get("p_uin") or cookies.get("uin") or "").lstrip("o0")
        if identity != str(int(account)) or not (cookies.get("p_skey") or cookies.get("skey")):
            raise QzoneReadError("尚未取得 Bot 小号可用的空间登录态")
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if k in COOKIE_KEYS),
            "Referer": f"https://user.qzone.qq.com/{account}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(
            timeout=20, trust_env=False, transport=self.transport, follow_redirects=False
        ) as client:

            async def get(url, params=None, *, allow_home=False):
                async with client.stream("GET", url, headers=headers, params=params) as response:
                    location = urlsplit(response.headers.get("location", ""))
                    self.trace.append(
                        {
                            "path": urlsplit(url).path,
                            "status": response.status_code,
                            "redirect_host": location.hostname,
                            "redirect_path": location.path,
                        }
                    )
                    if allow_home and response.status_code in (301, 302, 303, 307, 308) and location.hostname == "user.qzone.qq.com" and location.path.rstrip("/") == "/" + str(account):
                        return ""
                    if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                        raise QzoneReadError("空间登录态需要更新，自动浏览本轮已停止")
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise QzoneReadError("空间单页响应超过读取上限")
                        chunks.append(chunk)
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
                    callback = re.search(r"([A-Za-z_$][\w.$]*)\s*\(\s*[\[{]", text)
                    self.trace[-1].update(content_type=response.headers.get("content-type", ""), bytes=size, html_title=title[1][:100] if title else None, callback=callback[1] if callback else None, login_page=bool(re.search(r"ptlogin|请先登录|登录超时", text[:20000], re.I)))
                    return text

            gtk = cookie_gtk(cookies)
            index = await get(INDEX, {"g_tk": gtk}, allow_home=True)
            token = ""
            try:
                payload = parse_index_html(index)
                posts = normalize_posts(payload, account)
                data = payload.get("data", payload)
                token = str(data.get("qzonetoken", "")) if isinstance(data, dict) else ""
                if posts:
                    return str(account), posts
            except ValueError:
                pass
            if "ptlogin" in index[:12000].lower() and "shine0callback" not in index:
                raise QzoneReadError("空间需要重新确认登录")
            params = {"g_tk": gtk, "format": "json", "attach_info": ""}
            if token:
                params["qzonetoken"] = token
            try:
                payload = decode_payload(await get(ACTIVE, params))
                active_ok = isinstance(payload, dict) and not any(payload.get(k) not in (None, 0, "0") for k in ("ret", "code", "err"))
            except (QzoneReadError, httpx.HTTPError):
                active_ok = False
            if not active_ok:
                payload = decode_payload(
                    await get(
                        LEGACY,
                        {
                            "uin": account,
                            "scope": 0,
                            "view": 1,
                            "filter": "all",
                            "flag": 1,
                            "applist": "all",
                            "pagenum": 1,
                            "format": "json",
                            "useutf8": 1,
                            "outputhtmlfeed": 1,
                            "g_tk": gtk,
                        },
                    )
                )
            if not isinstance(payload, dict) or any(
                payload.get(k) not in (None, 0, "0") for k in ("ret", "code", "err")
            ):
                raise QzoneReadError("空间接口未返回可读动态，请检查小号空间登录状态")
            return str(account), normalize_posts(payload, account)
