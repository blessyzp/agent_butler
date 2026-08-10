"""结构化提取 schema 单元测试 —— 纯 pydantic 校验，不连 Ollama，不碰 data/。

对比现状：旧的正则抠 JSON 方案里，priority/recurrence 越界值是靠
Butler._sane_priority/_sane_recurrence 事后静默降级为默认值；这里改成
schema 层面（Literal 约束）直接在构造阶段拒绝非法值，抛 ValidationError，
这是两种方案行为上的关键差异，用测试固化下来。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from isolation import check, report, section

from pydantic import ValidationError

from src.extraction import ChatExtraction, ProfileSignals, TaskExtraction

section("TaskExtraction 合法数据")
t = TaskExtraction(content="交周报", task_type="工作", due_at="2026-08-11T15:00:00+08:00",
                   priority=2, recurrence="weekly")
check("字段全部正确落入", t.content == "交周报" and t.priority == 2 and t.recurrence == "weekly")

t2 = TaskExtraction(content="随便记一下")
check("可选字段有默认值", t2.priority == 0 and t2.recurrence is None and t2.due_at is None)

section("TaskExtraction 越界值：schema 直接拒绝（不是静默降级）")
try:
    TaskExtraction(content="x", priority=9)
    check("priority=9 应抛 ValidationError", False)
except ValidationError:
    check("priority=9 应抛 ValidationError", True)

try:
    TaskExtraction(content="x", recurrence="yearly")
    check("recurrence='yearly' 应抛 ValidationError", False)
except ValidationError:
    check("recurrence='yearly' 应抛 ValidationError", True)

section("ProfileSignals")
p = ProfileSignals(tone_hint="warm", goals=["考过CPA"], notes=["讨厌被催"])
check("语气/目标/笔记都正确落入", p.tone_hint == "warm" and p.goals == ["考过CPA"])

try:
    ProfileSignals(tone_hint="angry")
    check("tone_hint 越界应抛 ValidationError", False)
except ValidationError:
    check("tone_hint 越界应抛 ValidationError", True)

p_empty = ProfileSignals()
check("全部留空时给出安全默认值", p_empty.tone_hint is None and p_empty.goals == [] and p_empty.notes == [])

section("ChatExtraction 整体结构")
c = ChatExtraction(
    reply="好的，已经记下了",
    tasks=[{"content": "交周报", "priority": 1, "recurrence": "weekly"}],
    profile_signals={"tone_hint": "playful"},
)
check("reply 正确", c.reply == "好的，已经记下了")
check("嵌套 tasks 正确构造为 TaskExtraction 实例",
      len(c.tasks) == 1 and isinstance(c.tasks[0], TaskExtraction) and c.tasks[0].priority == 1)
check("嵌套 profile_signals 正确构造", c.profile_signals.tone_hint == "playful")

c_empty = ChatExtraction(reply="没什么要记的")
check("tasks/profile_signals 缺省时给安全默认值",
      c_empty.tasks == [] and c_empty.profile_signals.tone_hint is None)

section("model_dump() 输出结构（供 Butler._persist 消费）")
dumped = c.model_dump()
check("model_dump 保留嵌套字典结构，_persist 可直接用 .get() 消费",
      isinstance(dumped["tasks"], list) and isinstance(dumped["tasks"][0], dict)
      and dumped["tasks"][0]["content"] == "交周报"
      and isinstance(dumped["profile_signals"], dict))

report()
