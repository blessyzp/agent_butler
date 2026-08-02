"""LLM 后端抽象层 —— 所有具体模型藏在统一接口后。

新增一种后端只需：实现 LLMBackend 的 chat()/embed()，在 _BACKENDS 注册。
新增一个模型只需：在 models.yaml 加一条，无需碰代码。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import requests

from .config import Config, get_config


# ═══════════════════════════════════════════════════════════
#  统一接口
# ═══════════════════════════════════════════════════════════
class LLMBackend(ABC):
    """所有后端遵守的稳定契约。上层只依赖这个接口，不认具体模型。"""

    def __init__(self, model_def: dict[str, Any], cfg: Config):
        self.model_def = model_def
        self.model = model_def["model"]
        self.cfg = cfg
        self.timeout = cfg.get("llm.timeout_seconds", 120)

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], **opts: Any) -> str:
        """messages: [{"role": "system|user|assistant", "content": "..."}] → 回复文本"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(f"{type(self).__name__} 不支持嵌入")

    def available(self) -> bool:
        """后端当前是否可用（服务在线 / 密钥就绪）。"""
        return True


# ═══════════════════════════════════════════════════════════
#  Ollama 本地后端
# ═══════════════════════════════════════════════════════════
class OllamaBackend(LLMBackend):
    def __init__(self, model_def: dict[str, Any], cfg: Config):
        super().__init__(model_def, cfg)
        self.host = cfg.get("llm.ollama_host", "http://localhost:11434")

    def chat(self, messages: list[dict[str, str]], **opts: Any) -> str:
        resp = requests.post(
            f"{self.host}/api/chat",
            json={"model": self.model, "messages": messages, "stream": False,
                  "options": opts or {}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            resp = requests.post(
                f"{self.host}/api/embeddings",
                json={"model": self.model, "prompt": t},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            out.append(resp.json()["embedding"])
        return out

    def available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            if r.status_code != 200:
                return False
            names = {m.get("name", "") for m in r.json().get("models", [])}
            # 允许 "qwen2.5:14b" 精确或前缀匹配
            return any(self.model == n or n.startswith(self.model) for n in names)
        except requests.RequestException:
            return False


# ═══════════════════════════════════════════════════════════
#  OpenAI 兼容后端（DeepSeek 及任何兼容网关）
# ═══════════════════════════════════════════════════════════
class OpenAICompatBackend(LLMBackend):
    """DeepSeek / 各类兼容 /chat/completions 的服务共用此后端。"""

    def __init__(self, model_def: dict[str, Any], cfg: Config,
                 base_url: str, api_key_env: str):
        super().__init__(model_def, cfg)
        self.base_url = base_url.rstrip("/")
        self.api_key = cfg.secret(api_key_env)
        self.api_key_env = api_key_env

    def chat(self, messages: list[dict[str, str]], **opts: Any) -> str:
        if not self.api_key:
            raise RuntimeError(f"缺少密钥 {self.api_key_env}，无法调用云端模型")
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "messages": messages, **opts},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def available(self) -> bool:
        return bool(self.api_key)


class DeepSeekBackend(OpenAICompatBackend):
    def __init__(self, model_def: dict[str, Any], cfg: Config):
        super().__init__(model_def, cfg,
                         base_url="https://api.deepseek.com",
                         api_key_env="DEEPSEEK_API_KEY")


# ═══════════════════════════════════════════════════════════
#  后端工厂：backend 名 → 类
# ═══════════════════════════════════════════════════════════
_BACKENDS = {
    "ollama": OllamaBackend,
    "deepseek": DeepSeekBackend,
    # "openai_compatible": 需要 base_url/key_env，见 build_backend 特判
}


def build_backend(model_id: str, cfg: Config | None = None) -> LLMBackend:
    """按模型 ID 从注册表构造后端实例。"""
    cfg = cfg or get_config()
    mdef = cfg.model_def(model_id)
    if not mdef:
        raise KeyError(f"models.yaml 中找不到模型: {model_id}")

    backend_name = mdef.get("backend")
    if backend_name == "openai_compatible":
        return OpenAICompatBackend(
            mdef, cfg,
            base_url=mdef.get("base_url", ""),
            api_key_env=mdef.get("api_key_env", ""),
        )
    cls = _BACKENDS.get(backend_name)
    if cls is None:
        raise ValueError(f"未知后端类型: {backend_name}（模型 {model_id}）")
    return cls(mdef, cfg)
