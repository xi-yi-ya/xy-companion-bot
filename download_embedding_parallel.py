"""Resume the verified model with bounded parallel HTTP ranges from a mirror."""

import concurrent.futures
import hashlib
import json
import math
import shutil
from pathlib import Path

import requests

root = Path(__file__).resolve().parent / "runtime/models/bge-small-zh-v1.5"
target = root / "model_optimized.onnx"
prefix = root / "model_optimized.onnx.part"
manifest = root / "segments.json"
size = 94781076
expected = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
if manifest.exists():
    offset = json.loads(manifest.read_text())["offset"]
else:
    offset = prefix.stat().st_size if prefix.exists() else 0
    manifest.write_text(json.dumps({"offset": offset}), "utf-8")
step = math.ceil((size - offset) / 8)
url = "https://hf-mirror.com/Qdrant/bge-small-zh-v1.5/resolve/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59/model_optimized.onnx"


def fetch(index):
    begin = offset + index * step
    end = min(size - 1, begin + step - 1)
    part = root / f"model.segment.{index}"
    have = part.stat().st_size if part.exists() else 0
    if have == end - begin + 1:
        return part
    session = requests.Session()
    session.trust_env = False
    with session.get(
        url, headers={"Range": f"bytes={begin + have}-{end}"}, stream=True, timeout=(15, 60)
    ) as response:
        response.raise_for_status()
        assert response.status_code == 206, "Mirror did not honor byte range"
        assert response.headers["Content-Range"].startswith(f"bytes {begin + have}-"), (
            "Unexpected response range"
        )
        with part.open("ab") as output:
            for block in response.iter_content(128 * 1024):
                output.write(block)
    assert part.stat().st_size == end - begin + 1, "Incomplete model segment"
    print(f"Model segment {index + 1}/8 complete", flush=True)
    return part


with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    parts = list(pool.map(fetch, range(8)))
assembled = root / "model.assembled"
with assembled.open("wb") as output:
    if offset:
        with prefix.open("rb") as source:
            shutil.copyfileobj(source, output)
    for part in parts:
        with part.open("rb") as source:
            shutil.copyfileobj(source, output)
assert assembled.stat().st_size == size
assert hashlib.sha256(assembled.read_bytes()).hexdigest() == expected, "Model checksum mismatch"
assembled.replace(target)
print("Full Chinese embedding model SHA256 verified", flush=True)
