"""验证 schema v1→v2 迁移：已有数据不丢、可反复执行、真实库 v1 能平滑升级。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import ROOT, check, report, section, setup

data = setup("butler_mig")
import src.versioning as V

db = data / "memory.db"

# ── 造一个货真价实的 v1 库：只跑 v1 迁移，塞入数据 ──
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
v1_only = [m for m in V.MIGRATIONS if m[0] == 1]
for _, stmts in v1_only:
    for s in stmts: conn.execute(s)
conn.execute("PRAGMA user_version = 1"); conn.commit()
conn.execute("INSERT INTO tasks(content_enc, task_type, status, due_at, created_at, reminded_count)"
             " VALUES('enc_payload_abc', '工作', 'pending', '2026-08-01T09:00:00+08:00',"
             " '2026-07-26T10:00:00+00:00', 3)")
conn.execute("INSERT INTO events(kind, detail_enc, created_at)"
             " VALUES('episode', 'enc_episode_xyz', '2026-07-26T10:00:00+00:00')")
conn.commit()
print(f"── 造出 v1 库：user_version={conn.execute('PRAGMA user_version').fetchone()[0]}，"
      f"1 任务 + 1 事件 ──", flush=True)
cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
check("v1 库确实没有 last_reminded_at 列", "last_reminded_at" not in cols_before)

section("执行迁移")
V.ensure_schema(conn)
check("user_version 升到 2", conn.execute("PRAGMA user_version").fetchone()[0] == 2)
cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
check("新增 last_reminded_at 列", "last_reminded_at" in cols)

t = conn.execute("SELECT * FROM tasks").fetchone()
check("原有任务数据完好（加密载荷未动）", t["content_enc"] == "enc_payload_abc", t["content_enc"])
check("原有字段值保留", t["task_type"] == "工作" and t["reminded_count"] == 3
      and t["due_at"] == "2026-08-01T09:00:00+08:00")
check("新列对老数据为 NULL（不是伪造时间）", t["last_reminded_at"] is None, t["last_reminded_at"])
check("事件表未受影响",
      conn.execute("SELECT detail_enc FROM events").fetchone()[0] == "enc_episode_xyz")

section("幂等性（反复调用不应报错/不重复加列）")
try:
    V.ensure_schema(conn); V.ensure_schema(conn)
    check("重复迁移不抛异常", True)
except Exception as e:
    check("重复迁移不抛异常", False, repr(e))
check("列没被加两次", sum(1 for r in conn.execute("PRAGMA table_info(tasks)")
                          if r["name"] == "last_reminded_at") == 1)
conn.close()

section("真实库现状（只读检查，不修改）")
real = ROOT / "data" / "memory.db"
if real.exists():
    rc = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
    ver = rc.execute("PRAGMA user_version").fetchone()[0]
    n_task = rc.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    n_ev = rc.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    has_col = any(r[1] == "last_reminded_at" for r in rc.execute("PRAGMA table_info(tasks)"))
    print(f"  真实库 user_version={ver}，{n_task} 任务 / {n_ev} 事件，"
          f"last_reminded_at 列{'已有' if has_col else '尚无（下次启动自动迁移）'}", flush=True)
    rc.close()
else:
    print("  真实库尚未创建（还没正式跑过管家）", flush=True)

report()
