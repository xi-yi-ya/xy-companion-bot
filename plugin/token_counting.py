"""Official local DeepSeek tokenizer for planning; API usage remains authoritative."""

import json
import math
import os
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer


class TokenCounter:
    def __init__(self):
        path = Path(
            os.environ.get(
                "COMPANION_TOKENIZER_PATH",
                Path(__file__).resolve().parents[1]
                / "runtime/models/deepseek-tokenizer/tokenizer.json",
            )
        )
        if os.environ.get("COMPANION_TOKENIZER_PATH") and not path.is_file():
            raise FileNotFoundError(
                "Production context planning requires the official DeepSeek tokenizer"
            )
        self.tokenizer = Tokenizer.from_file(str(path)) if path.is_file() else None
        self.method = "official_deepseek_v4" if self.tokenizer else "character_fallback"

    def text(self, text):
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, default=str)
        if not text:
            return 0
        if self.tokenizer:
            return len(self.tokenizer.encode(text, add_special_tokens=False).ids)
        chinese = sum("\u4e00" <= c <= "\u9fff" for c in text)
        return math.ceil(chinese * 0.8 + (len(text) - chinese) * 0.4) + 1

    def content(self, value):
        if isinstance(value, list):
            return sum(
                12288
                if item.get("type") in ("image_url", "image")
                else self.text(item.get("text", item))
                for item in value
            )
        return self.text(value or "")

    def messages(self, messages):
        return 12 + sum(
            8 + self.content(m.get("content")) + self.text(m.get("tool_calls", []))
            for m in messages
        )


@lru_cache(maxsize=1)
def counter():
    return TokenCounter()


def tokens(text):
    return counter().text(text)


def split_text(text, limit):
    codec = counter()
    if tokens(text) <= limit:
        return [text]
    if codec.tokenizer:
        offsets = codec.tokenizer.encode(text, add_special_tokens=False).offsets
        pieces = []
        for index in range(0, len(offsets), max(1, limit - 80)):
            end = min(len(offsets), index + limit)
            pieces.append(text[offsets[index][0] : offsets[end - 1][1]])
        return [piece for piece in pieces if piece]
    pieces = []
    while text:
        size = min(len(text), limit)
        while tokens(text[:size]) > limit:
            size = max(1, int(size * 0.8))
        pieces.append(text[:size])
        text = text[size:]
    return pieces
