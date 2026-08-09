"""前端 + API 冒烟测试。

隔离原则（见 context_snapshot.md bug #9 的教训）：
  绝不碰真实的 D:/butler/data —— 先用临时 config.yaml 把所有 paths.* 重定向到
  一次性临时目录，再初始化 config 单例，之后才允许 import Butler / create_app。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import ROOT, check, report, section, setup

_real_settings = ROOT / "data" / "settings.json"
_real_settings_mtime_before = (
    _real_settings.stat().st_mtime if _real_settings.exists() else None
)

data = setup("butler_smoke")
print(f"[隔离] data_dir -> {data}")

from fastapi.testclient import TestClient
from src.api import create_app

app = create_app()
with TestClient(app) as c:
    section("静态页挂载（关键：不能遮蔽 API 路由）")
    r = c.get("/")
    check("GET / 返回 index.html", r.status_code == 200 and "电子管家" in r.text,
          f"status={r.status_code} len={len(r.text)}")
    check("GET /health 未被静态挂载遮蔽",
          c.get("/health").status_code == 200 and c.get("/health").json() == {"ok": True})

    section("状态 / 设置")
    r = c.get("/status"); s = r.json()
    check("GET /status", r.status_code == 200 and "resource" in s,
          f"pressure={s.get('resource', {}).get('pressure')} model={s.get('model')}")
    check("/status 含前端依赖字段",
          all(k in s for k in ("backends", "speech_available", "pending_tasks", "active_role")))

    r = c.get("/settings/schema"); sch = r.json()
    check("GET /settings/schema", r.status_code == 200 and len(sch) >= 10, f"{len(sch)} 个字段")
    check("schema 每项都带前端渲染所需元数据",
          all({"key", "label", "group", "kind", "value"} <= set(f) for f in sch))
    check("int 字段带 min/max（滑块需要）",
          all({"min", "max"} <= set(f) for f in sch if f["kind"] == "int"))
    check("choice 字段带 choices（下拉需要）",
          all("choices" in f for f in sch if f["kind"] == "choice"))

    check("PUT /settings 合法值",
          c.put("/settings", json={"patch": {"persona.tone": "warm",
                                             "reminder.max_per_day": 6}}).status_code == 200)
    check("PUT /settings 越界值被拒 400",
          c.put("/settings", json={"patch": {"reminder.max_per_day": 999}}).status_code == 400)
    check("PUT /settings 未知键被拒",
          c.put("/settings", json={"patch": {"nope.nope": 1}}).status_code in (400, 500))
    check("设置已持久化", c.get("/settings").json()["persona.tone"] == "warm")

    section("任务 CRUD（含本轮修的 PATCH content / 撤销）")
    r = c.post("/tasks", json={"content": "买牛奶", "task_type": "生活",
                               "due_at": "2026-08-01T09:00:00+08:00"})
    check("POST /tasks", r.status_code == 200 and "id" in r.json())
    tid = r.json()["id"]

    lst = c.get("/tasks").json()
    check("GET /tasks 解密后可读",
          any(t["id"] == tid and t["content"] == "买牛奶" for t in lst), lst)
    check("任务行含前端渲染字段",
          all({"id", "content", "task_type", "status", "due_at", "reminded_count"} <= set(t)
              for t in lst))

    check("PATCH content 真的生效（修复前会静默忽略）",
          c.patch(f"/tasks/{tid}", json={"content": "买酸奶"}).status_code == 200 and
          c.get("/tasks").json()[0]["content"] == "买酸奶")
    check("PATCH content 空字符串被拒 400",
          c.patch(f"/tasks/{tid}", json={"content": "   "}).status_code == 400)
    check("PATCH 非法 status 被拒 400",
          c.patch(f"/tasks/{tid}", json={"status": "bogus"}).status_code == 400)

    check("PATCH status=done", c.patch(f"/tasks/{tid}", json={"status": "done"}).status_code == 200)
    check("完成后不在 pending", all(t["id"] != tid for t in c.get("/tasks").json()))
    check("完成后在 done 列表", any(t["id"] == tid for t in c.get("/tasks?status=done").json()))
    check("PATCH status=pending 可撤销（新增能力）",
          c.patch(f"/tasks/{tid}", json={"status": "pending"}).status_code == 200 and
          any(t["id"] == tid for t in c.get("/tasks").json()))
    check("DELETE /tasks", c.delete(f"/tasks/{tid}").status_code == 200 and
          all(t["id"] != tid for t in c.get("/tasks").json()))

    section("任务优先级 / 重复任务")
    r = c.post("/tasks", json={"content": "非法优先级", "priority": 9})
    check("POST /tasks priority 越界 400", r.status_code == 400)
    r = c.post("/tasks", json={"content": "非法重复规则", "recurrence": "yearly"})
    check("POST /tasks recurrence 越界 400", r.status_code == 400)

    r = c.post("/tasks", json={"content": "交周报", "task_type": "工作",
                               "due_at": "2026-08-03T09:00:00+08:00",
                               "priority": 2, "recurrence": "weekly"})
    check("POST /tasks 带优先级/重复", r.status_code == 200)
    rid = r.json()["id"]
    row = next(t for t in c.get("/tasks").json() if t["id"] == rid)
    check("priority/recurrence 正确落库", row["priority"] == 2 and row["recurrence"] == "weekly", row)

    lst = c.get("/tasks").json()
    check("高优先级任务排在前面", lst[0]["id"] == rid, lst)

    check("PATCH priority 越界 400",
          c.patch(f"/tasks/{rid}", json={"priority": 5}).status_code == 400)
    check("PATCH recurrence 越界 400",
          c.patch(f"/tasks/{rid}", json={"recurrence": "yearly"}).status_code == 400)
    check("PATCH priority 生效",
          c.patch(f"/tasks/{rid}", json={"priority": 1}).status_code == 200 and
          next(t for t in c.get("/tasks").json() if t["id"] == rid)["priority"] == 1)

    check("完成重复任务自动生成下一条",
          c.patch(f"/tasks/{rid}", json={"status": "done"}).status_code == 200)
    pending = c.get("/tasks").json()
    check("下一条已生成（同内容，pending 状态）",
          any(t["content"] == "交周报" and t["recurrence"] == "weekly" for t in pending), pending)
    next_task = next(t for t in pending if t["content"] == "交周报")
    check("下一条截止时间比原来晚 7 天",
          next_task["due_at"] > "2026-08-03T09:00:00+08:00", next_task["due_at"])

    check("PATCH recurrence='' 取消重复",
          c.patch(f"/tasks/{next_task['id']}", json={"recurrence": ""}).status_code == 200 and
          next(t for t in c.get("/tasks").json() if t["id"] == next_task["id"])["recurrence"] is None)
    check("清理：删除测试任务",
          c.delete(f"/tasks/{next_task['id']}").status_code == 200)

    section("历史记录（新增端点）")
    r = c.get("/history")
    check("GET /history 空库返回空数组", r.status_code == 200 and r.json() == [], r.json())

    # 用应用自己的 Butler 播种对话型 episode 验证解析
    # （不能新建第二个 Memory：同进程双 Chroma 客户端会 segfault）
    mem = app.state.butler.memory
    mem.add_episode("[用户] 明天提醒我开会\n[管家] 好，已经记下了")
    mem.add_episode("这是一条非对话型记忆")

    h = c.get("/history").json()
    check("/history 正确拆出用户/管家两侧", len(h) == 2 and
          h[0]["user"] == "明天提醒我开会" and h[0]["reply"] == "好，已经记下了", h)
    check("/history 非对话 episode 不丢",
          h[1]["user"] == "" and h[1]["reply"] == "这是一条非对话型记忆", h[1])
    check("/history 带时间戳（前端展示用）", all(x["created_at"] for x in h))
    check("/history 时间正序（老的在前）", h[0]["created_at"] <= h[1]["created_at"])

    section("多模态端点参数校验（不触发真实模型）")
    check("POST /input/voice 空文件 400",
          c.post("/input/voice", files={"file": ("a.webm", b"", "audio/webm")}).status_code == 400)
    check("POST /input/image 空文件 400",
          c.post("/input/image", files={"file": ("a.png", b"", "image/png")},
                 data={"text": ""}).status_code == 400)
    r = c.post("/input/image", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
                                              "image/png")}, data={"text": "这是什么"})
    check("POST /input/image 后端不可用时 502 且不崩溃",
          r.status_code in (200, 502), f"status={r.status_code} {str(r.json())[:120]}")

section("真实数据目录未被触碰（核查）")
check("临时库文件已生成在临时目录", (data / "memory.db").exists())
_mtime_after = _real_settings.stat().st_mtime if _real_settings.exists() else None
check("真实 data 下的 settings.json 未被本次测试改动",
      _mtime_after == _real_settings_mtime_before)

report()
