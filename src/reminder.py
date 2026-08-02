"""提醒与监督引擎 —— 智能计算时机、按画像语气催办、到期追责。

不是简单定时器：提前量 = f(拖延历史, 截止时间)；尊重免打扰时段与每日上限；
到期未完成会主动追问（监督闭环）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config, get_config
from .memory import Memory
from .notify import Notifier
from .profile import Profile

# 不同语气的提醒开场白模板（LLM 不可用时的兜底）
_TONE_FALLBACK = {
    "gentle":  "嘿，顺带提醒你一下~",
    "playful": "老板，那个「{task}」是不是还没动？👀",
    "serious": "提醒：任务「{task}」需要处理了。",
    "warm":    "记得照顾好自己，顺便看看「{task}」哦。",
}

# LLM 生成器签名： (system_prompt, user_prompt) -> str
LLMFn = Callable[[str, str], str]

# 逾期追问的退避阶梯（小时）：第 1 次逾期提醒后等 1h，再等 3h、6h…
# 没有退避的话，每个 tick（默认 5 分钟）都会重发同一条。
_OVERDUE_BACKOFF_HOURS = (1, 3, 6, 12, 24)


class ReminderEngine:
    def __init__(self,
                 memory: Memory,
                 profile: Profile,
                 notifier: Notifier,
                 llm_fn: LLMFn,
                 settings=None,
                 cfg: Config | None = None):
        self.memory = memory
        self.profile = profile
        self.notifier = notifier
        self.llm_fn = llm_fn
        self.cfg = cfg or get_config()
        # 运行时可调设置（免打扰/上限/语气等）；None 时惰性创建
        if settings is None:
            from .settings import Settings
            settings = Settings(self.cfg)
        self.settings = settings
        self.scheduler = BackgroundScheduler()

    def _persona_name(self) -> str:
        return self.settings.get("persona.name")

    # ── 生命周期 ──
    def start(self) -> None:
        interval = int(self.settings.get("reminder.tick_minutes"))
        self.scheduler.add_job(self.tick, "interval", minutes=interval,
                               id="reminder_tick", replace_existing=True)
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ── 提前量计算 ──
    def compute_lead_minutes(self, task: dict) -> int:
        base = int(self.settings.get("reminder.default_lead_minutes"))
        score = self.profile.procrastination_score(task.get("task_type"))
        # 越易拖延，提前量越大（最多放大 3 倍）
        return int(base * (1 + 2 * score))

    # ── 每次 tick 的核心逻辑 ──
    def tick(self) -> None:
        now = datetime.now().astimezone()  # 带本地时区，避免与 UTC 时间比较报错
        if self._in_quiet_hours(now):
            return
        if self._reached_daily_cap(now):
            return

        self._check_due_tasks(now)
        if self.settings.get("reminder.supervision_enabled"):
            self._check_overdue(now)
            self._check_silence(now)

    def _check_due_tasks(self, now: datetime) -> None:
        # 找出"进入提前提醒窗口"的任务
        for task in self.memory.list_tasks("pending"):
            due = _parse_iso(task.get("due_at"))
            if not due:
                continue
            lead = self.compute_lead_minutes(task)
            remind_at = due - timedelta(minutes=lead)
            if remind_at <= now <= due and task["reminded_count"] == 0:
                if not self._fire(task, urgency="normal", now=now):
                    return          # 配额用尽，本轮不再发

    def _check_overdue(self, now: datetime) -> None:
        """逾期追问 —— 必须退避。

        每个 tick（默认 5 分钟）无条件重发会在 20 分钟内打满当天配额，
        既轰炸用户，又把额度挤占光导致真正临期的任务当天提醒不了。
        """
        for task in self.memory.list_tasks("pending"):
            due = _parse_iso(task.get("due_at"))
            if not due or now <= due:
                continue
            last = _parse_iso(task.get("last_reminded_at"))
            if last is not None:
                # 已提醒过 N 次 → 等待越来越久再追问
                idx = min(max(task["reminded_count"] - 1, 0),
                          len(_OVERDUE_BACKOFF_HOURS) - 1)
                if now - last < timedelta(hours=_OVERDUE_BACKOFF_HOURS[idx]):
                    continue
            if not self._fire(task, urgency="overdue", now=now):
                return              # 配额用尽，本轮不再发

    def _check_silence(self, now: datetime) -> None:
        last = self.memory.last_activity_iso()
        if not last:
            return
        last_dt = _parse_iso(last)
        if not last_dt:
            return
        gap_h = (now - last_dt).total_seconds() / 3600
        threshold = int(self.settings.get("reminder.silence_check_hours"))
        # 每天最多问候一次沉默
        if gap_h >= threshold and not self._greeted_today(now):
            title = f"{self._persona_name()}想你了"
            body = self._compose("silence", "", urgency="normal")
            self.notifier.send(title, body)
            self.memory.log_event("silence_greet")

    # ── 发送单条提醒 ──
    def _fire(self, task: dict, urgency: str, now: datetime) -> bool:
        """发一条提醒。返回 False 表示当日配额已用尽（调用方应停止本轮）。"""
        if self._reached_daily_cap(now):
            return False
        title = f"{self._persona_name()}提醒"
        body = self._compose("task", task["content"], urgency=urgency)
        self.notifier.send(title, body)
        self.memory.log_event("reminder_sent", task["content"])
        self.memory.bump_reminded(task["id"])
        return True

    # ── 语气化文案（LLM 生成，失败回落模板）──
    def _compose(self, kind: str, task_text: str, urgency: str) -> str:
        tone = self.settings.get("persona.tone")
        emoji = "可用 emoji" if self.settings.get("persona.use_emoji") else "不要用 emoji"
        try:
            system = (f"你是用户的电子管家，名叫{self._persona_name()}。"
                      f"用『{tone}』的语气，{emoji}，一句话，简短自然，像真人。")
            if kind == "task":
                user = (f"提醒用户去做这件事：{task_text}。"
                        f"紧急程度：{'已逾期，需要认真督促' if urgency=='overdue' else '临近截止'}。")
            else:  # silence
                user = "用户好久没消息了，用关心但不啰嗦的一句话问候，并轻轻问问近况。"
            msg = self.llm_fn(system, user).strip()
            return msg or self._fallback(tone, task_text)
        except Exception:
            return self._fallback(tone, task_text)

    def _fallback(self, tone: str, task_text: str) -> str:
        tmpl = _TONE_FALLBACK.get(tone, _TONE_FALLBACK["playful"])
        return tmpl.format(task=task_text or "那件事")

    # ── 免打扰 / 每日上限 ──
    def _in_quiet_hours(self, now: datetime) -> bool:
        start, end = self.settings.quiet_hours()
        h = now.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end  # 跨午夜

    def _reached_daily_cap(self, now: datetime) -> bool:
        """当日配额。从事件日志实时统计，而不是内存计数 —— 内存计数一重启
        就清零，等于重启一次就能再轰炸一轮。"""
        cap = int(self.settings.get("reminder.max_per_day"))
        return self.memory.count_events_since("reminder_sent", _midnight(now)) >= cap

    def _greeted_today(self, now: datetime) -> bool:
        return self.memory.count_events_since("silence_greet", _midnight(now)) > 0


def _midnight(now: datetime) -> datetime:
    """今天本地零点（配额按用户所在时区的自然日计）。"""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        # 无时区的时间按本地时区解释（用户输入的截止时间通常是本地时间）
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None
