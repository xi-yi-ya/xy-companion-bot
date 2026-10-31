"""Pinned local Chinese ONNX embeddings, evaluated outside the event loop."""

import asyncio
from pathlib import Path


class LocalEmbedding:
    model_id = "BAAI/bge-small-zh-v1.5@46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"

    def __init__(self, model_dir):
        from fastembed import TextEmbedding

        if not (Path(model_dir) / "model_optimized.onnx").exists():
            raise FileNotFoundError("Run download_embedding.py before starting vector memory")
        self.model = TextEmbedding(
            model_name="BAAI/bge-small-zh-v1.5",
            specific_model_path=str(model_dir),
            local_files_only=True,
            threads=2,
            providers=["CPUExecutionProvider"],
        )
        self.lock = asyncio.Lock()

    async def encode(self, texts):
        async with self.lock:
            return await asyncio.to_thread(lambda: [v.tolist() for v in self.model.embed(texts)])
