"""Download only JSON data from the official DeepSeek tokenizer archive."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx

URL = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"
SHA256 = "e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495"


def download():
    target = Path(__file__).resolve().parent / "runtime/models/deepseek-tokenizer"
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.get(URL)
        response.raise_for_status()
        data = response.content
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError("Official tokenizer archive changed; re-verify its version and checksum")
    archive = zipfile.ZipFile(io.BytesIO(data))
    target.mkdir(parents=True, exist_ok=True)
    for name in archive.namelist():
        basename = Path(name).name
        if basename in ("tokenizer.json", "tokenizer_config.json"):
            (target / basename).write_bytes(archive.read(name))
    (target / "source.json").write_text(json.dumps({"url": URL, "sha256": SHA256}), "utf-8")
    return target / "tokenizer.json"


if __name__ == "__main__":
    print(download())
