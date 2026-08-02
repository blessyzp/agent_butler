"""验证 reminder 修复：逾期退避、配额跨重启、画像不再被提醒污染。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import ROOT, check, report, section, setup

data = setup("butler_rem2")

from src.crypto import Cipher
from src.memory import Memory
from src.profile import Profile
from src.reminder import ReminderEngine
from src.settings import Settings

mem = Memory(); prof = Profile(); st = Settings()
st.update({"reminder.quiet_start": 3, "reminder.quiet_end": 4, "reminder.max_per_day": 8})
sent = []
class FakeNotifier:
    def send(self, title, body): sent.append(body); return True
def new_engine():
    return ReminderEngine(mem, prof, FakeNotifier(), llm_fn=lambda s, u: "该做事了", settings=st)

now = datetime.now().astimezone()
t1 = mem.add_task("交季度报告", "工作", (now - timedelta(hours=2)).isoformat(timespec="seconds"))
t2 = mem.add_task("交水电费", "生活", (now - timedelta(hours=3)).isoformat(timespec="seconds"))

section("逾期退避（修复前：每 tick 重发，20 分钟打满 8 条）")
eng = new_engine()
for i in range(5):
    eng.tick()
check("5 次 tick 只发 2 条（每任务 1 条），不再轰炸", len(sent) == 2, f"实际 {len(sent)} 条")
check("reminded_count 各为 1",
      [t["reminded_count"] for t in mem.list_tasks("pending")] == [1, 1])
check("last_reminded_at 已写入（schema v2 新列）",
      all(t["last_reminded_at"] for t in mem.list_tasks("pending")))

section("画像不再被提醒污染（修复前 20 分钟被打到 1.00）")
check("提醒不写拖延分", prof.data["habits"]["procrastination_types"] == {},
      prof.data["habits"]["procrastination_types"])
check("提前量停在无数据基准 96 分钟（=60×(1+2×0.3)），而非被污染后的 180",
      eng.compute_lead_minutes({"task_type": "工作"}) == 96,
      eng.compute_lead_minutes({"task_type": "工作"}))

section("退避到期后应当再追问一次")
import sqlite3
mem.conn.execute("UPDATE tasks SET last_reminded_at=? WHERE id=?",
                 ((now - timedelta(hours=2)).astimezone().isoformat(), t1))
mem.conn.commit()
before = len(sent)
new_engine().tick()
check("超过 1 小时退避窗口后重新追问 1 次", len(sent) - before == 1, f"+{len(sent)-before}")

section("每日配额跨重启（修复前内存计数，重启即清零）")
for i in range(3, 12):
    mem.log_event("reminder_sent", f"填充{i}")
fresh = new_engine()                       # 模拟重启：全新引擎实例
before = len(sent)
mem.conn.execute("UPDATE tasks SET last_reminded_at=NULL")   # 解除退避
mem.conn.commit()
fresh.tick()
check("新实例仍认得当天已发满，不再发送", len(sent) == before, f"+{len(sent)-before}")

section("拖延分双向（修复前 record_reliable 全项目没人调用）")
from src.butler import Butler
b = Butler.__new__(Butler)                 # 只用 complete_task，不启动完整管家
b.memory, b.profile = mem, prof
b.complete_task(t1)                        # 逾期完成 → 拖延
t3 = mem.add_task("按时交的活", "工作", (now + timedelta(hours=5)).isoformat(timespec="seconds"))
b.complete_task(t3)                        # 提前完成 → 守时
hab = prof.data["habits"]
check("逾期完成记拖延", hab["procrastination_types"].get("工作") == 1, hab["procrastination_types"])
check("按时完成记守时（原先永远是 0）", hab["reliable_types"].get("工作") == 1, hab["reliable_types"])
score = prof.procrastination_score("工作")
check("拖延分成为真正的比例值，不再只有 0.3 / 1.0", abs(score - 0.5) < 1e-9, f"score={score}")

mem.close()
report()
