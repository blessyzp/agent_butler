"""模型调度器 —— 资源压力 → 角色选择 → 后端，带防抖与可用性回退。

压力映射：
  low      → chat_large  (本地 14B)
  medium   → chat_small  (本地 7B)
  high     → chat_small  (本地 7B，勉强)
  critical → chat_cloud  (DeepSeek 云端)

回退：若目标角色后端不可用（如 Ollama 没开 / 云端没配密钥），
自动沿"本地大→本地小→云端"链条寻找可用者。
"""
from __future__ import annotations

import time

from .config import Config, get_config
from .registry import ModelRegistry, get_registry
from .resource_monitor import ResourceMonitor

_PRESSURE_TO_ROLE = {
    "low": "chat_large",
    "medium": "chat_small",
    "high": "chat_small",
    "critical": "chat_cloud",
}

# 回退链：从紧张到宽松都试一遍
_FALLBACK_CHAIN = ["chat_large", "chat_small", "chat_cloud"]


class ModelScheduler:
    def __init__(self, monitor: ResourceMonitor,
                 registry: ModelRegistry | None = None,
                 cfg: Config | None = None):
        self.monitor = monitor
        self.registry = registry or get_registry()
        self.cfg = cfg or get_config()
        self.cooldown = self.cfg.get("resource.switch_cooldown_seconds", 60)
        self._current_role: str | None = None
        self._last_switch = 0.0

    def _now(self) -> float:
        return time.time()

    def choose_role(self) -> str:
        """结合压力 + 防抖，得出目标角色（尚未做可用性回退）。"""
        snap = self.monitor.get()
        target = _PRESSURE_TO_ROLE.get(snap.pressure_level, "chat_cloud")

        if self._current_role is None:
            self._current_role = target
            self._last_switch = self._now()
            return target

        if target != self._current_role:
            # 冷却期内维持现状，避免边界抖动反复切换
            if self._now() - self._last_switch < self.cooldown:
                return self._current_role
            self._log_switch(self._current_role, target, snap.pressure_level)
            self._current_role = target
            self._last_switch = self._now()

        return self._current_role

    def resolve(self) -> tuple[str, object]:
        """返回 (实际使用的角色, 后端实例)，含可用性回退。"""
        desired = self.choose_role()

        # 从 desired 起，沿回退链找第一个可用后端
        order = self._fallback_order(desired)
        for role in order:
            model_id = self.registry.role_model_id(role)
            if not model_id:
                continue
            try:
                backend = self.registry.backend(model_id)
                if backend.available():
                    return role, backend
            except Exception:
                continue

        raise RuntimeError(
            "没有可用的 LLM 后端：请启动 Ollama 或在 .env 配置 DEEPSEEK_API_KEY"
        )

    def _fallback_order(self, desired: str) -> list[str]:
        # 把 desired 放最前，其余按标准链补齐
        rest = [r for r in _FALLBACK_CHAIN if r != desired]
        return [desired] + rest

    def _log_switch(self, old: str, new: str, pressure: str) -> None:
        snap = self.monitor.get()
        print(f"[调度] 模型切换 {old} → {new} "
              f"(压力={pressure}, VRAM剩余={snap.vram_available_mb}MB, "
              f"RAM剩余={snap.ram_available_gb}GB)")

    def status(self) -> str:
        role, backend = self.resolve()
        model_id = self.registry.role_model_id(role)
        return f"当前角色 {role} → 模型 {model_id}"
