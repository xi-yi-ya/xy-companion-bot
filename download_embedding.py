"""Download a pinned public ONNX model; no remote Python code is executed."""

import hashlib
import os
from pathlib import Path

import requests

REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
ROOT = Path(__file__).resolve().parent / "runtime/models/bge-small-zh-v1.5"
ROOT.mkdir(parents=True, exist_ok=True)
FILES = [
    "config.json",
    "model_optimized.onnx",
    "ort_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
]
EXPECTED = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
session = requests.Session()
endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
for name in FILES:
    path = ROOT / name
    if path.exists() and (
        name != "model_optimized.onnx" or hashlib.sha256(path.read_bytes()).hexdigest() == EXPECTED
    ):
        continue
    partial = path.with_suffix(path.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with session.get(
        f"{endpoint}/Qdrant/bge-small-zh-v1.5/resolve/{REVISION}/{name}",
        headers=headers,
        stream=True,
        timeout=(15, 90),
    ) as response:
        response.raise_for_status()
        resume = offset and response.status_code == 206
        written = offset if resume else 0
        milestone = written // (10 * 1024 * 1024)
        with partial.open("ab" if resume else "wb") as output:
            for block in response.iter_content(1024 * 512):
                output.write(block)
                written += len(block)
                if written // (10 * 1024 * 1024) > milestone:
                    milestone = written // (10 * 1024 * 1024)
                    print(f"{name}: {written // 1024 // 1024} MiB", flush=True)
    if name == "model_optimized.onnx":
        assert hashlib.sha256(partial.read_bytes()).hexdigest() == EXPECTED, (
            "Model checksum mismatch"
        )
    partial.replace(path)
    print(f"Downloaded {name}", flush=True)
print("Pinned Chinese embedding model ready", flush=True)
