"""验证 WAL + 备份/恢复：未 checkpoint 的事务能否被备份带走、恢复后数据是否正确。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import ROOT, check, report, section, setup

data = setup("butler_bak")

import src.config as C
from src.backup import BackupManager
from src.memory import Memory

mem = Memory()
mode = mem.conn.execute("PRAGMA journal_mode").fetchone()[0]
check("WAL 模式已启用", mode.lower() == "wal", mode)

mem.add_task("备份前就存在的任务", "工作", None)
mem.add_task("第二条", "生活", None)
db = Path(C.get_config().get("paths.memory_db"))
check("产生了 -wal 文件（说明事务确实先落在 WAL）",
      db.with_name(db.name + "-wal").exists())

section("快照（不做 checkpoint，考验 backup API）")
bm = BackupManager()
snap = bm.snapshot("test")
snap_db = snap / db.name
check("快照含 memory.db", snap_db.exists())
sc = sqlite3.connect(f"file:{snap_db}?mode=ro", uri=True)
n = sc.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
check("快照里有全部 2 条任务（裸复制 .db 会是 0 条）", n == 2, f"{n} 条")
sc.close()

section("改动数据后恢复")
mem.add_task("快照之后新增的，恢复后应消失", "工作", None)
check("当前库有 3 条", len(mem.list_tasks("pending")) == 3)
mem.close()

bm.restore(snap)
# 注：restore 清掉旧 -wal 后，下一个连接会因 WAL 模式立刻重建一个新的 —— 这是
# 正常的。真正要保证的是"恢复出来的库里没有夹带旧事务"，由下面的数据断言覆盖。
check("恢复后立即读到的是快照内容，未被旧 -wal 覆盖",
      sqlite3.connect(f"file:{db}?mode=ro", uri=True)
      .execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2)

mem2 = Memory()
tasks = [t["content"] for t in mem2.list_tasks("pending")]
check("恢复回 2 条", len(tasks) == 2, tasks)
check("解密仍然正确（加密载荷完好）", "备份前就存在的任务" in tasks, tasks)
check("快照后新增的那条已消失", "快照之后新增的，恢复后应消失" not in tasks)
check("restore 前自动做了 pre_restore 兜底备份",
      any("pre_restore" in p.name for p in bm.list_snapshots()),
      [p.name for p in bm.list_snapshots()])
mem2.close()

report()
