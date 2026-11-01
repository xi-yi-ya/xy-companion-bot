"""One-shot, own-account text publication; no likes, comments or automatic retries."""

import re

import httpx

from .qzone_http import COOKIE_KEYS, QzoneReadError, decode_payload
from .qzone_read.parser import cookie_gtk

PUBLISH = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)


class PublicationRejected(ValueError):
    pass


class QzonePublisher:
    def __init__(self, credentials, *, transport=None):
        self.credentials, self.transport = credentials, transport

    async def prepare(self, account):
        actual, cookies = await self.credentials()
        identity = str(cookies.get("p_uin") or cookies.get("uin") or "").lstrip("o0")
        if (
            str(actual) != str(account)
            or identity != str(int(account))
            or not (cookies.get("p_skey") or cookies.get("skey"))
        ):
            raise QzoneReadError("Bot 空间登录态或账号已变化")
        return str(account), cookies

    async def send(self, prepared, text):
        account, cookies = prepared
        identity = str(cookies.get("p_uin") or cookies.get("uin") or "").lstrip("o0")
        if identity != str(int(account)):
            raise PublicationRejected("发布账号校验未通过")
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if k in COOKIE_KEYS),
            "Referer": f"https://user.qzone.qq.com/{account}",
            "Origin": "https://user.qzone.qq.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36",
        }
        data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": account,
            "code_version": "1",
            "richval": "",
            "issyncweibo": "0",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{account}",
        }
        async with httpx.AsyncClient(
            timeout=25, trust_env=False, transport=self.transport, follow_redirects=False
        ) as client:
            async with client.stream(
                "POST", PUBLISH, params={"g_tk": cookie_gtk(cookies)}, headers=headers, data=data
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ValueError("发布回执过大，结果需要确认")
        payload = decode_payload(body.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise ValueError("发布回执格式无法确认")
        for key in ("code", "ret", "err"):
            if payload.get(key) not in (None, 0, "0"):
                try:
                    code = str(int(payload[key]))
                except (ValueError, TypeError):
                    code = "未知"
                raise PublicationRejected("空间接口拒绝发布（代码 " + code + "）")
        fields = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        post_id = str(fields.get("tid") or fields.get("fid") or payload.get("tid") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", post_id):
            raise ValueError("未取得新说说 ID，不能确认发布结果")
        return {"post_id": post_id, "url": f"https://user.qzone.qq.com/{account}/mood/{post_id}"}
