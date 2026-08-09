"""主控编排 —— 把资源监控、模型调度、记忆、画像、提醒串成一个管家。

对话流：检索记忆 → 注入画像 → 按资源选后端 → 生成回复 →
提取任务/画像信号 → 落库 → 记录活跃节律。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .config import get_config
from .crypto import Cipher
from .memory import Memory
from .notify import Notifier
from .profile import Profile
from .registry import get_registry
from .resource_monitor import ResourceMonitor, format_snapshot
from .reminder import ReminderEngine
from .scheduler_model import ModelScheduler
from .settings import Settings
from .speech import Transcriber
from .vision import VisionHelper

_EXTRACT_INSTRUCTION = """
回答用户后，另起一行输出一个 JSON 代码块（```json ... ```），包含你从本轮对话中提取的信息：
{
  "tasks": [{"content": "要做的事", "task_type": "类型如 工作/生活/学习", "due_at": "ISO时间或null",
             "priority": "0=普通/1=重要/2=紧急，拿不准填0", "recurrence": "daily/weekly/monthly/null"}],
  "profile_signals": {"tone_hint": "若用户表达了语气偏好则填 gentle/playful/serious/warm 否则null",
                       "goals": ["新出现的长期目标"], "notes": ["值得记住的偏好/情绪"]}
}
due_at 必须以上面【当前时间】为基准换算成绝对时间，并带上同样的时区偏移
（例如当前时间是 2026-07-26T20:00:00+08:00 时，"明天下午三点" = "2026-07-27T15:00:00+08:00"）。
绝对不要凭记忆猜年份；说不准具体时刻就填 null，不要编一个。
recurrence 只有用户明确说"每天/每周/每月"之类才填，否则填 null。
没有可提取内容时用空数组/ null。JSON 块之前的文字才是给用户看的回复。
""".strip()

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 重复任务：完成时按此规则推算下一次的 due_at
_RECURRENCE_STEP = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),  # 月份天数不固定，用 30 天近似，足够日常提醒场景
}


class Butler:
    def __init__(self):
        self.cfg = get_config()
        self.cipher = Cipher.instance()
        self.registry = get_registry()
        self.monitor = ResourceMonitor(self.cfg)
        self.scheduler = ModelScheduler(self.monitor, self.registry, self.cfg)
        self.settings = Settings(self.cfg)
        self.memory = Memory(self.cfg, self.cipher, self.registry)
        self.profile = Profile(self.cfg, self.cipher)
        self.notifier = Notifier(self.cfg)
        self.transcriber = Transcriber(self.cfg)
        self.vision = VisionHelper(self.registry, self.cfg)
        self.reminder = ReminderEngine(
            self.memory, self.profile, self.notifier, self._raw_chat,
            settings=self.settings, cfg=self.cfg,
        )

    # ── 生命周期 ──
    def start(self) -> None:
        self.monitor.start()
        self.reminder.start()

    def shutdown(self) -> None:
        self.reminder.shutdown()
        self.monitor.stop()
        self.memory.close()

    # ── 底层：一次无提取的 LLM 调用（供提醒引擎复用）──
    def _raw_chat(self, system: str, user: str) -> str:
        _, backend = self.scheduler.resolve()
        return backend.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    # ── 对话主入口 ──
    def chat(self, user_input: str) -> str:
        # 记录活跃小时（画像节律）
        self.profile.record_activity_hour(datetime.now().hour)
        self.memory.log_event("user_msg", user_input)

        memories = self.memory.retrieve(user_input, k=5)
        mem_block = "\n".join(f"- {m}" for m in memories) if memories else "（暂无相关记忆）"

        tone = self.settings.get("persona.tone")
        emoji = "可用 emoji" if self.settings.get("persona.use_emoji") else "不要用 emoji"
        # 必须把当前时间喂给模型：否则它会拿训练数据里的年份猜 due_at
        # （实测会算出 2023 年），提醒引擎收到的截止时间全是错的。
        now = datetime.now().astimezone()
        now_str = f"{now.isoformat(timespec='seconds')}（{_WEEKDAYS[now.weekday()]}）"
        system = (
            f"你是用户的私人电子管家，名叫{self.settings.get('persona.name')}。\n"
            f"用『{tone}』的语气，{emoji}。\n"
            f"【当前时间】{now_str}\n"
            f"【用户画像】\n{self.profile.summary()}\n"
            f"【相关记忆】\n{mem_block}\n"
            f"请自然对话。\n{_EXTRACT_INSTRUCTION}"
        )

        role, backend = self.scheduler.resolve()
        raw = backend.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ])

        reply, extracted = self._split_extraction(raw)

        # 落库：任务 + 画像信号 + 情景记忆
        self._persist(user_input, reply, extracted)
        return reply

    # ── 多模态输入：转成文字后复用 chat() 的记忆/画像/提取流程 ──
    def chat_with_voice(self, audio_bytes: bytes) -> dict:
        transcript = self.transcriber.transcribe_bytes(audio_bytes)
        if not transcript:
            raise RuntimeError("未能从音频中识别出内容")
        reply = self.chat(transcript)
        return {"transcript": transcript, "reply": reply}

    def chat_with_image(self, image_bytes: bytes, user_text: str = "") -> dict:
        caption = self.vision.describe(image_bytes)
        combined = f"[图片内容] {caption}"
        if user_text.strip():
            combined += f"\n[用户说] {user_text.strip()}"
        reply = self.chat(combined)
        return {"caption": caption, "reply": reply}

    # ── 解析 LLM 附带的 JSON 提取块 ──
    def _split_extraction(self, raw: str) -> tuple[str, dict]:
        m = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if not m:
            return raw.strip(), {}
        reply = raw[:m.start()].strip()
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = {}
        return (reply or raw.strip()), data

    @staticmethod
    def _sane_due(raw) -> str | None:
        """兜底校验模型给的 due_at。

        即便 system prompt 里已给出当前时间，小模型仍可能编出错误年份。宁可存成
        "没有截止时间"（照样看得见的待办），也不能存一个早已过期的时间 —— 那会让
        提醒引擎把它当逾期任务反复追问。
        """
        if raw in (None, "", "null", "None"):
            return None
        try:
            due = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if due.tzinfo is None:                      # 裸时间按本地时区解释
            due = due.astimezone()
        # 允许略微过去（用户在补记刚发生的事），但明显穿越的一律丢弃
        if due < datetime.now().astimezone() - timedelta(days=1):
            return None
        return due.isoformat(timespec="seconds")

    @staticmethod
    def _sane_priority(raw) -> int:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            return 0
        return p if p in (0, 1, 2) else 0

    @staticmethod
    def _sane_recurrence(raw) -> str | None:
        return raw if raw in _RECURRENCE_STEP else None

    def _persist(self, user_input: str, reply: str, extracted: dict) -> None:
        for t in extracted.get("tasks", []) or []:
            content = (t or {}).get("content")
            if content:
                self.memory.add_task(
                    content,
                    task_type=t.get("task_type"),
                    due_at=self._sane_due(t.get("due_at")),
                    priority=self._sane_priority(t.get("priority")),
                    recurrence=self._sane_recurrence(t.get("recurrence")),
                )
        signals = extracted.get("profile_signals") or {}
        # 语气偏好统一由 Settings 承载（前端可见可改），从对话学到就更新它
        hint = signals.pop("tone_hint", None)
        if hint not in (None, "null"):
            try:
                self.settings.set("persona.tone", hint)
            except (KeyError, ValueError):
                pass
        self.profile.absorb_signals(signals)

        # 情景记忆（真相源 + 向量）
        self.memory.add_episode(f"[用户] {user_input}\n[管家] {reply}")

    # ── 任务完成（同时更新拖延画像）──
    def complete_task(self, task_id: int) -> dict | None:
        """标记完成，并按"是否按时"记一笔画像。

        拖延分必须在这里记，不能在提醒时记：按"被追问了几次"计分会让同一条
        任务每个 tick 都 +1，20 分钟就把某个类别打到满分，而且只增不减
        （record_reliable 原先全项目没人调用，分数实际只有 0.3 和 1.0 两档）。
        """
        task = self.memory.complete_task(task_id)
        if not task:
            return None
        ttype = task.get("task_type") or "其他"
        due = self._sane_parse(task.get("due_at"))
        if due is not None:
            if datetime.now().astimezone() > due:
                self.profile.record_procrastination(ttype)
            else:
                self.profile.record_reliable(ttype)
        self._spawn_next_recurrence(task, due)
        return task

    def _spawn_next_recurrence(self, task: dict, due: datetime | None) -> None:
        """重复任务完成后自动生成下一条，避免"每周一交周报"要手动重建。"""
        rule = task.get("recurrence")
        step = _RECURRENCE_STEP.get(rule)
        if step is None:
            return
        # 以原定截止时间为基准推算下一次；没有截止时间就从"现在"起算
        base = due or datetime.now().astimezone()
        next_due = base + step
        self.memory.add_task(
            task["content"],
            task_type=task.get("task_type"),
            due_at=next_due.isoformat(timespec="seconds"),
            priority=task.get("priority") or 0,
            recurrence=rule,
        )

    @staticmethod
    def _sane_parse(raw) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        return dt if dt.tzinfo else dt.astimezone()

    # ── 状态诊断 ──
    def status(self) -> str:
        snap = self.monitor.get()
        avail = self.registry.availability()
        avail_str = ", ".join(f"{k}:{'✓' if v else '✗'}" for k, v in avail.items())
        pending = len(self.memory.list_tasks("pending"))
        return (
            "── 管家状态 ──\n"
            f"{format_snapshot(snap)}\n"
            f"{self.scheduler.status()}\n"
            f"后端可用性: {avail_str}\n"
            f"语音识别: {'✓' if self.transcriber.available() else '✗（pip install faster-whisper）'}\n"
            f"待办任务: {pending} 项\n"
            f"画像:\n{self.profile.summary()}"
        )
