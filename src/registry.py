"""模型注册表 —— 按角色解析并缓存后端实例。

上层代码只说"给我 chat_large / embed 角色的后端"，不关心背后是哪个模型、
哪个后端。换模型时改 config.yaml 的 roles 或 models.yaml 即可。
"""
from __future__ import annotations

from typing import Any

from .config import Config, get_config
from .llm import LLMBackend, build_backend


class ModelRegistry:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self._cache: dict[str, LLMBackend] = {}

    # ── 按模型 ID 取后端（带缓存）──
    def backend(self, model_id: str) -> LLMBackend:
        if model_id not in self._cache:
            self._cache[model_id] = build_backend(model_id, self.cfg)
        return self._cache[model_id]

    # ── 按角色取后端 ──
    def for_role(self, role: str) -> LLMBackend:
        model_id = self.cfg.model_for_role(role)
        if not model_id:
            raise KeyError(f"config.yaml 未给角色 '{role}' 绑定模型")
        return self.backend(model_id)

    def role_model_id(self, role: str) -> str | None:
        return self.cfg.model_for_role(role)

    # ── 嵌入模型的元信息（迁移兼容校验用）──
    def embed_model_id(self) -> str:
        mid = self.cfg.model_for_role("embed")
        if not mid:
            raise KeyError("未配置 embed 角色")
        return mid

    def embed_dim(self) -> int | None:
        mid = self.embed_model_id()
        return self.cfg.model_def(mid).get("dim")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.for_role("embed").embed(texts)

    # ── 可用性快照（供调度/诊断）──
    def availability(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for role in ("chat_large", "chat_small", "chat_cloud", "vision", "embed"):
            mid = self.cfg.model_for_role(role)
            if not mid:
                continue
            try:
                result[role] = self.backend(mid).available()
            except Exception:
                result[role] = False
        return result


_instance: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    global _instance
    if _instance is None:
        _instance = ModelRegistry()
    return _instance
