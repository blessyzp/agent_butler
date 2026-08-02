"""用户画像 —— 加密 JSON 存储，随对话增量更新。

画像是模型中立的结构化数据（与真相源同级），换任何 LLM 都不受影响。
LLM 只负责从对话里"提取信号"，画像的存储与演化由本模块掌管。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, get_config
from .crypto import Cipher

# 画像结构版本（便于未来迁移字段）
PROFILE_VERSION = 1

_DEFAULT_PROFILE = {
    "version": PROFILE_VERSION,
    "persona": {
        "tone": "playful",          # gentle / playful / serious / warm
        "use_emoji": True,
        "reply_length": "short",    # short / detailed
    },
    "rhythm": {
        "active_hours": [],          # 观察到的活跃小时，如 [8,9,21,22]
        "hour_histogram": {},        # 小时 -> 出现次数
    },
    "habits": {
        "procrastination_types": {}, # 任务类型 -> 拖延次数
        "reliable_types": {},        # 任务类型 -> 按时完成次数
    },
    "goals": [],                     # 长期目标（用户明确陈述）
    "notes": [],                     # 其他画像信号
}


class Profile:
    def __init__(self, cfg: Config | None = None, cipher: Cipher | None = None):
        self.cfg = cfg or get_config()
        self.cipher = cipher or Cipher.instance()
        self.path = Path(self.cfg.get("paths.profile_file"))
        self.data = self._load()

    # ── 加载 / 保存（加密）──
    def _load(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(_DEFAULT_PROFILE))  # 深拷贝
        try:
            enc = self.path.read_text(encoding="utf-8")
            raw = self.cipher.decrypt(enc)
            data = json.loads(raw)
            return self._migrate(data)
        except Exception as e:
            print(f"ⓘ 画像读取失败（{e}），使用默认画像。")
            return json.loads(json.dumps(_DEFAULT_PROFILE))

    def save(self) -> None:
        raw = json.dumps(self.data, ensure_ascii=False)
        self.path.write_text(self.cipher.encrypt(raw), encoding="utf-8")

    def _migrate(self, data: dict) -> dict:
        # 结构版本迁移：补齐缺失字段，绝不删除已有数据
        if data.get("version", 0) < PROFILE_VERSION:
            merged = json.loads(json.dumps(_DEFAULT_PROFILE))
            _deep_merge(merged, data)
            merged["version"] = PROFILE_VERSION
            return merged
        return data

    # ── 语气偏好 ──
    @property
    def tone(self) -> str:
        return self.data["persona"].get("tone", "playful")

    @property
    def use_emoji(self) -> bool:
        return self.data["persona"].get("use_emoji", True)

    def set_tone(self, tone: str) -> None:
        self.data["persona"]["tone"] = tone
        self.save()

    # ── 活跃节律 ──
    def record_activity_hour(self, hour: int) -> None:
        hist = self.data["rhythm"]["hour_histogram"]
        hist[str(hour)] = hist.get(str(hour), 0) + 1
        # 取出现最多的若干小时作为活跃时段
        top = Counter({int(k): v for k, v in hist.items()}).most_common(6)
        self.data["rhythm"]["active_hours"] = sorted(h for h, _ in top)
        self.save()

    def is_active_hour(self, hour: int) -> bool:
        active = self.data["rhythm"]["active_hours"]
        return (hour in active) if active else True  # 无数据时不设限

    # ── 拖延建模 ──
    def record_procrastination(self, task_type: str) -> None:
        d = self.data["habits"]["procrastination_types"]
        d[task_type] = d.get(task_type, 0) + 1
        self.save()

    def record_reliable(self, task_type: str) -> None:
        d = self.data["habits"]["reliable_types"]
        d[task_type] = d.get(task_type, 0) + 1
        self.save()

    def procrastination_score(self, task_type: str | None) -> float:
        """0~1，越高越易拖延，用于计算提醒提前量。"""
        if not task_type:
            return 0.3
        p = self.data["habits"]["procrastination_types"].get(task_type, 0)
        r = self.data["habits"]["reliable_types"].get(task_type, 0)
        total = p + r
        return (p / total) if total else 0.3

    # ── 目标 ──
    def add_goal(self, goal: str) -> None:
        if goal not in self.data["goals"]:
            self.data["goals"].append(goal)
            self.save()

    # ── 批量吸收 LLM 提取的信号 ──
    def absorb_signals(self, signals: dict) -> None:
        """
        signals 形如：
          {"tone_hint": "serious",
           "goals": ["考过CPA"],
           "notes": ["讨厌被催"]}
        """
        if not signals:
            return
        if signals.get("tone_hint"):
            self.data["persona"]["tone"] = signals["tone_hint"]
        for g in signals.get("goals", []) or []:
            if g not in self.data["goals"]:
                self.data["goals"].append(g)
        for n in signals.get("notes", []) or []:
            if n not in self.data["notes"]:
                self.data["notes"].append(n)
        self.save()

    # ── 供 system prompt 注入的摘要 ──
    def summary(self) -> str:
        rhythm = self.data["rhythm"]["active_hours"]
        goals = self.data["goals"]
        notes = self.data["notes"][-5:]
        parts = [f"语气偏好: {self.tone}（emoji: {'开' if self.use_emoji else '关'}）"]
        if rhythm:
            parts.append(f"活跃时段(小时): {rhythm}")
        if goals:
            parts.append(f"长期目标: {'; '.join(goals)}")
        if notes:
            parts.append(f"注意: {'; '.join(notes)}")
        return "\n".join(parts)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
