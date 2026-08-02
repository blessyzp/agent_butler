"""_sane_due 边界 + 真实对话复测（验证 due_at 日期修复）。"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import ROOT, check, report, section, setup

data = setup("butler_due")

from src.butler import Butler

section("_sane_due 单元边界")
sd = Butler._sane_due
now = datetime.now().astimezone()
future = (now + timedelta(days=1)).isoformat(timespec="seconds")

check("None / 'null' / 空 → None",
      sd(None) is None and sd("null") is None and sd("") is None)
check("垃圾字符串 → None（不抛异常）", sd("下周三") is None)
check("模型幻觉的 2023 年 → None（本轮修复的核心）",
      sd("2023-04-15T15:00:00Z") is None)
check("合法未来时间 → 保留", sd(future) is not None, sd(future))
check("裸时间按本地时区补全",
      (r := sd((now + timedelta(hours=3)).replace(tzinfo=None).isoformat())) is not None
      and ("+" in r or "-" in r[10:]), r)
check("刚过去 1 小时 → 保留（补记刚发生的事）",
      sd((now - timedelta(hours=1)).isoformat(timespec="seconds")) is not None)
check("过去 3 天 → None", sd((now - timedelta(days=3)).isoformat()) is None)
check("Z 后缀 UTC 正确解析",
      sd((now + timedelta(days=2)).astimezone().isoformat()) is not None)

section("真实对话复测：'明天下午三点' 应落在明天")
b = Butler(); b.start()
try:
    t0 = time.time()
    reply = b.chat("帮我记一下，明天下午三点要交季度报告")
    print(f"  耗时 {time.time()-t0:.1f}s  回复：{reply[:120]}", flush=True)
    tasks = b.memory.list_tasks("pending")
    for t in tasks:
        print(f"  · {t['content']}  due={t['due_at']}  type={t['task_type']}", flush=True)
    check("提取到任务", len(tasks) >= 1)
    if tasks:
        due = tasks[0]["due_at"]
        check("due_at 非空且不是幻觉年份", bool(due), due)
        if due:
            d = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone()
            tomorrow = (now + timedelta(days=1)).date()
            check("due_at 落在明天", d.date() == tomorrow, f"{d.date()} vs 期望 {tomorrow}")
            check("due_at 是下午三点", d.hour == 15, f"hour={d.hour}")
finally:
    b.shutdown()

report()
