"""可调设置 —— 用户运行时能改的项，与静态 config.yaml 分离。

设计：
  • 每个字段带元数据（类型/范围/选项/标签/分组），前端据此自动渲染控件
  • 默认值来自 config.yaml；用户覆盖持久化到 data/settings.json（明文，非机密）
  • get/set 带校验；改完即时生效（各模块每次读取都走 Settings）
  • 未来加一项设置 = 往 FIELDS 加一条，前端与校验自动生效
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config, get_config


# ── 可调字段定义（前端渲染 + 校验的单一真相源）──
# kind: int | bool | str | choice ；choice 需 choices；int 需 min/max
def _fields(cfg: Config) -> list[dict[str, Any]]:
    q = cfg.get("reminder.quiet_hours", [23, 7])
    return [
        # ── 提醒 ──
        {"key": "reminder.quiet_start", "label": "免打扰开始(小时)", "group": "提醒",
         "kind": "int", "min": 0, "max": 23, "default": q[0]},
        {"key": "reminder.quiet_end", "label": "免打扰结束(小时)", "group": "提醒",
         "kind": "int", "min": 0, "max": 23, "default": q[1]},
        {"key": "reminder.max_per_day", "label": "每日提醒上限", "group": "提醒",
         "kind": "int", "min": 0, "max": 50,
         "default": cfg.get("reminder.max_per_day", 8)},
        {"key": "reminder.tick_minutes", "label": "检查频率(分钟)", "group": "提醒",
         "kind": "int", "min": 1, "max": 60,
         "default": cfg.get("reminder.tick_minutes", 5)},
        {"key": "reminder.default_lead_minutes", "label": "默认提前量(分钟)",
         "group": "提醒", "kind": "int", "min": 5, "max": 1440,
         "default": cfg.get("reminder.default_lead_minutes", 60)},
        {"key": "reminder.supervision_enabled", "label": "开启监督追问",
         "group": "提醒", "kind": "bool",
         "default": cfg.get("reminder.supervision_enabled", True)},
        {"key": "reminder.silence_check_hours", "label": "沉默多久后问候(小时)",
         "group": "提醒", "kind": "int", "min": 1, "max": 72,
         "default": cfg.get("reminder.silence_check_hours", 5)},
        # ── 语气人格 ──
        {"key": "persona.tone", "label": "语气", "group": "人格",
         "kind": "choice", "choices": ["gentle", "playful", "serious", "warm"],
         "default": cfg.get("persona.tone", "playful")},
        {"key": "persona.use_emoji", "label": "使用表情", "group": "人格",
         "kind": "bool", "default": cfg.get("persona.use_emoji", True)},
        {"key": "persona.name", "label": "管家称呼", "group": "人格",
         "kind": "str", "default": cfg.get("persona.name", "小管家")},
    ]


class Settings:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self._schema = {f["key"]: f for f in _fields(self.cfg)}
        self.path = Path(self.cfg.get("paths.data_dir")) / "settings.json"
        self._overrides: dict[str, Any] = self._load()

    # ── 读取（覆盖优先，否则默认）──
    def get(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key in self._schema:
            return self._schema[key]["default"]
        raise KeyError(f"未知设置项: {key}")

    def quiet_hours(self) -> tuple[int, int]:
        return int(self.get("reminder.quiet_start")), int(self.get("reminder.quiet_end"))

    # ── 写入（校验后持久化）──
    def set(self, key: str, value: Any) -> Any:
        if key not in self._schema:
            raise KeyError(f"未知设置项: {key}")
        value = self._validate(self._schema[key], value)
        self._overrides[key] = value
        self._save()
        return value

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """批量更新，返回生效后的完整设置。全部校验通过才落盘。"""
        validated = {k: self._validate(self._schema[k], v)
                     for k, v in patch.items() if k in self._schema}
        unknown = [k for k in patch if k not in self._schema]
        if unknown:
            raise KeyError(f"未知设置项: {unknown}")
        self._overrides.update(validated)
        self._save()
        return self.as_dict()

    def _validate(self, field: dict, value: Any) -> Any:
        kind = field["kind"]
        if kind == "int":
            v = int(value)
            lo, hi = field["min"], field["max"]
            if not (lo <= v <= hi):
                raise ValueError(f"{field['key']} 需在 [{lo}, {hi}]，收到 {v}")
            return v
        if kind == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on", "是")
            return bool(value)
        if kind == "choice":
            if value not in field["choices"]:
                raise ValueError(f"{field['key']} 需为 {field['choices']} 之一")
            return value
        if kind == "str":
            s = str(value).strip()
            if not s:
                raise ValueError(f"{field['key']} 不能为空")
            return s
        return value

    # ── 供前端 ──
    def schema(self) -> list[dict[str, Any]]:
        """返回字段元数据 + 当前值，前端据此渲染表单。"""
        out = []
        for f in self._schema.values():
            item = dict(f)
            item["value"] = self.get(f["key"])
            out.append(item)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {k: self.get(k) for k in self._schema}

    # ── 持久化 ──
    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._overrides, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
