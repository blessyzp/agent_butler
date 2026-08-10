"""结构化任务提取 —— 用 LangChain 的 with_structured_output() 替代正则抠 JSON 代码块。

原方案：system prompt 里拜托模型"回复后另起一行输出 ```json 代码块"，回来后
用正则去抠、`json.loads()` 解析，抠不到/解析失败就静默退化成空字典。这套方案
脆弱在于完全依赖模型"记得"严格遵守格式，格式漂移（多写一句话、少个反引号）
就悄悄丢提取结果。

这里改成一次模型调用直接产出 schema 约束的结构化对象：reply（自然语言回复）+
tasks + profile_signals 一起出，不再是"先生成自由文本，再解析"的两段式，
不增加额外的模型调用延迟。

实测选型：langchain-ollama 的 with_structured_output() 有三种 method，
'function_calling' 在本地 qwen2.5:7b 上不稳定（工具调用经常直接不触发，
tool_calls 为空、退化成纯文本），'json_schema'（Ollama 原生结构化输出 API，
用 /api/chat 的 format 参数）测试下来稳定可靠，因此采用 json_schema。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .config import Config


class TaskExtraction(BaseModel):
    content: str
    task_type: Optional[str] = None
    due_at: Optional[str] = None
    # schema 层面直接约束取值范围；语义校验（是不是模型编的过去年份）仍由
    # butler.py 的 _sane_due 负责 —— schema 管"类型对不对"，_sane_due 管
    # "这个值语义上说得通吗"，两层校验职责不同，不能互相替代。
    priority: Literal[0, 1, 2] = 0
    recurrence: Optional[Literal["daily", "weekly", "monthly"]] = None


class ProfileSignals(BaseModel):
    tone_hint: Optional[Literal["gentle", "playful", "serious", "warm"]] = None
    goals: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ChatExtraction(BaseModel):
    reply: str
    tasks: list[TaskExtraction] = Field(default_factory=list)
    profile_signals: ProfileSignals = Field(default_factory=ProfileSignals)


_cache: dict[str, object] = {}


def build_structured_llm(model_id: str, cfg: Config):
    """按 model_id（models.yaml 里的 ID）构造一个绑定了 ChatExtraction schema
    的 LangChain 聊天模型，按 model_id 缓存（同 registry.py 的缓存风格）。
    """
    if model_id in _cache:
        return _cache[model_id]

    mdef = cfg.model_def(model_id)
    if not mdef:
        raise KeyError(f"models.yaml 中找不到模型: {model_id}")

    backend_name = mdef.get("backend")
    if backend_name == "ollama":
        from langchain_ollama import ChatOllama

        host = cfg.get("llm.ollama_host", "http://localhost:11434")
        llm = ChatOllama(
            model=mdef["model"],
            base_url=host,
            timeout=cfg.get("llm.timeout_seconds", 120),
        )
    elif backend_name in ("deepseek", "openai_compatible"):
        from langchain_openai import ChatOpenAI

        if backend_name == "deepseek":
            base_url, api_key_env = "https://api.deepseek.com", "DEEPSEEK_API_KEY"
        else:
            base_url = mdef.get("base_url", "")
            api_key_env = mdef.get("api_key_env", "")
        api_key = cfg.secret(api_key_env)
        if not api_key:
            raise RuntimeError(f"缺少密钥 {api_key_env}，无法构造结构化云端模型")
        llm = ChatOpenAI(
            model=mdef["model"],
            base_url=base_url,
            api_key=api_key,
            timeout=cfg.get("llm.timeout_seconds", 120),
        )
    else:
        raise ValueError(f"未知后端类型: {backend_name}（模型 {model_id}）")

    structured = llm.with_structured_output(ChatExtraction, method="json_schema")
    _cache[model_id] = structured
    return structured
