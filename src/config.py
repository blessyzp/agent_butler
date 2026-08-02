"""配置加载：合并 config.yaml + models.yaml + .env，提供全局单例访问。

设计要点：模型定义集中在 models.yaml（注册表），config.yaml 只按角色引用
模型 ID。换模型 = 改 yaml，代码零改动。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """配置单例。结构化参数来自 yaml，密钥来自 .env。"""

    def __init__(self, config_path: str | None = None):
        load_dotenv(_PROJECT_ROOT / ".env")

        path = Path(config_path) if config_path else _PROJECT_ROOT / "config.yaml"
        self._cfg: dict[str, Any] = self._load_yaml(path)

        # 模型注册表（可缺失，缺失时 registry 用内置默认）
        models_path = _PROJECT_ROOT / "models.yaml"
        self._models: dict[str, Any] = (
            self._load_yaml(models_path).get("models", {})
            if models_path.exists()
            else {}
        )

        self._ensure_dirs()

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # ── 结构化参数访问（点号路径，如 "llm.roles.chat_large"）──
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._cfg
        for part in dotted_key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    # ── 模型注册表访问 ──
    def model_def(self, model_id: str) -> dict[str, Any]:
        """按 ID 返回模型定义（来自 models.yaml）。"""
        return self._models.get(model_id, {})

    def model_for_role(self, role: str) -> str | None:
        """按角色（chat_large / embed / ...）返回配置绑定的模型 ID。"""
        return self.get(f"llm.roles.{role}")

    @property
    def all_models(self) -> dict[str, Any]:
        return self._models

    # ── 密钥访问（来自环境变量）──
    @staticmethod
    def secret(key: str, default: str | None = None) -> str | None:
        val = os.environ.get(key, default)
        return val if val else default

    def has_deepseek(self) -> bool:
        return bool(self.secret("DEEPSEEK_API_KEY"))

    def _ensure_dirs(self) -> None:
        for key in ("paths.data_dir", "paths.vector_dir"):
            d = self.get(key)
            if d:
                Path(d).mkdir(parents=True, exist_ok=True)


_instance: Config | None = None


def get_config() -> Config:
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance
