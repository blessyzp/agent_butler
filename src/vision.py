"""图像理解 —— 预处理（压缩）后送本地视觉模型（MiniCPM-V via Ollama）。

压缩是为了不让手机原图（几千万像素）把显存/耗时打爆：统一转 RGB、
按最长边限制缩放、转 JPEG 再 base64，PIL 缺失时退化为原图直传。
"""
from __future__ import annotations

import base64
import io

from .config import Config, get_config
from .registry import ModelRegistry


class VisionHelper:
    def __init__(self, registry: ModelRegistry, cfg: Config | None = None):
        self.registry = registry
        self.cfg = cfg or get_config()

    def _prepare_b64(self, image_bytes: bytes) -> str:
        try:
            from PIL import Image
        except ImportError:
            return base64.b64encode(image_bytes).decode()

        max_dim = int(self.cfg.get("vision.max_dimension", 1280))
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()

    def describe(self, image_bytes: bytes,
                 prompt: str = "请用中文描述这张图片的内容，如果图中有文字请一并读出。") -> str:
        backend = self.registry.for_role("vision")
        b64 = self._prepare_b64(image_bytes)
        messages = [{"role": "user", "content": prompt, "images": [b64]}]
        return backend.chat(messages)

    def available(self) -> bool:
        try:
            return self.registry.for_role("vision").available()
        except Exception:
            return False
