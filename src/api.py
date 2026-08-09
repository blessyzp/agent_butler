"""HTTP API 层 —— 前端（网页/小程序/桌面）的统一后端。

仅绑定 localhost，默认不对外暴露（管家数据敏感）。若需远程访问，
走 WireGuard/内网穿透，切勿直接公网开放。

端点：
  GET  /health                健康检查
  GET  /status                资源/模型/待办状态
  GET  /settings              当前设置值
  GET  /settings/schema       设置字段元数据（前端渲染表单用）
  PUT  /settings              批量更新设置
  POST /chat                  发一句话，返回回复
  GET  /history               最近对话回合（前端刷新后恢复聊天记录）
  POST /input/voice           上传音频 → 转写 + 对话回复
  POST /input/image           上传图片(+可选文字) → 图片描述 + 对话回复
  GET  /tasks                 待办列表（可 ?status=done）
  POST /tasks                 新建任务
  PATCH /tasks/{id}           更新（完成/撤销/改内容）
  DELETE /tasks/{id}          删除
  GET  /                      前端页面（src/static/index.html）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .butler import Butler


# ── 请求体模型 ──
class ChatIn(BaseModel):
    message: str


class SettingsIn(BaseModel):
    patch: dict[str, Any]


class TaskIn(BaseModel):
    content: str
    task_type: str | None = None
    due_at: str | None = None
    priority: int = 0                       # 0=普通 1=重要 2=紧急
    recurrence: str | None = None           # daily / weekly / monthly / null


class TaskPatch(BaseModel):
    status: str | None = None       # done / pending
    content: str | None = None
    priority: int | None = None
    recurrence: str | None = None   # 传空字符串 "" 表示取消重复

def create_app() -> FastAPI:
    app = FastAPI(title="电子管家 API", version="0.1.0")
    butler = Butler()
    butler.start()
    # 暴露给测试/未来路由复用。注意：同进程内不要再新建第二个 Memory
    # （两个 Chroma 客户端指向同一 persist 目录会导致进程崩溃）。
    app.state.butler = butler

    @app.on_event("shutdown")
    def _shutdown() -> None:
        butler.shutdown()

    # ── 健康 / 状态 ──
    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/status")
    def status() -> dict:
        snap = butler.monitor.get()
        try:
            role, _ = butler.scheduler.resolve()
        except Exception:
            role = None  # 无可用后端（Ollama 未开且未配云端）
        return {
            "resource": {
                "vram_total_mb": snap.vram_total_mb,
                "vram_available_mb": snap.vram_available_mb,
                "ram_total_gb": snap.ram_total_gb,
                "ram_available_gb": snap.ram_available_gb,
                "cpu_percent": snap.cpu_percent,
                "pressure": snap.pressure_level,
                "gpu_present": snap.gpu_present,
            },
            "active_role": role,
            "model": butler.registry.role_model_id(role) if role else None,
            "backends": butler.registry.availability(),
            "speech_available": butler.transcriber.available(),
            "pending_tasks": len(butler.memory.list_tasks("pending")),
        }

    # ── 设置 ──
    @app.get("/settings")
    def get_settings() -> dict:
        return butler.settings.as_dict()

    @app.get("/settings/schema")
    def get_settings_schema() -> list[dict]:
        return butler.settings.schema()

    @app.put("/settings")
    def put_settings(body: SettingsIn) -> dict:
        try:
            return butler.settings.update(body.patch)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── 对话 ──
    @app.post("/chat")
    def chat(body: ChatIn) -> dict:
        if not body.message.strip():
            raise HTTPException(status_code=400, detail="message 不能为空")
        try:
            return {"reply": butler.chat(body.message)}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"模型调用失败: {e}")

    @app.get("/history")
    def history(limit: int = 30) -> list[dict]:
        """最近的对话回合。真相源是 episodes（格式 "[用户] x\\n[管家] y"）。"""
        out = []
        for ep in butler.memory.recent_episodes(max(1, min(limit, 200))):
            user, sep, reply = ep["text"].partition("\n[管家] ")
            if not sep:      # 非对话型 episode（如手工写入的记忆），整条当回复展示
                out.append({"created_at": ep["created_at"], "user": "", "reply": ep["text"]})
                continue
            out.append({
                "created_at": ep["created_at"],
                "user": user.removeprefix("[用户] "),
                "reply": reply,
            })
        return out

    # ── 多模态输入 ──
    @app.post("/input/voice")
    async def input_voice(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="音频为空")
        try:
            return butler.chat_with_voice(data)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"语音处理失败: {e}")

    @app.post("/input/image")
    async def input_image(file: UploadFile = File(...), text: str = Form("")) -> dict:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="图片为空")
        try:
            return butler.chat_with_image(data, text)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"图片处理失败: {e}")

    # ── 任务 CRUD ──
    @app.get("/tasks")
    def list_tasks(status: str = "pending") -> list[dict]:
        return butler.memory.list_tasks(status)

    @app.post("/tasks")
    def create_task(body: TaskIn) -> dict:
        if body.priority not in (0, 1, 2):
            raise HTTPException(status_code=400, detail="priority 只能是 0/1/2")
        if body.recurrence not in (None, "daily", "weekly", "monthly"):
            raise HTTPException(status_code=400, detail="recurrence 只能是 daily/weekly/monthly")
        tid = butler.memory.add_task(body.content, body.task_type, body.due_at,
                                     priority=body.priority, recurrence=body.recurrence)
        return {"id": tid}

    @app.patch("/tasks/{task_id}")
    def patch_task(task_id: int, body: TaskPatch) -> dict:
        if body.content is not None:
            content = body.content.strip()
            if not content:
                raise HTTPException(status_code=400, detail="content 不能为空")
            butler.memory.update_task_content(task_id, content)
        if body.priority is not None:
            if body.priority not in (0, 1, 2):
                raise HTTPException(status_code=400, detail="priority 只能是 0/1/2")
            butler.memory.update_task_priority(task_id, body.priority)
        if body.recurrence is not None:
            recurrence = body.recurrence or None    # "" → 取消重复
            if recurrence not in (None, "daily", "weekly", "monthly"):
                raise HTTPException(status_code=400, detail="recurrence 只能是 daily/weekly/monthly")
            butler.memory.update_task_recurrence(task_id, recurrence)
        if body.status == "done":
            butler.complete_task(task_id)      # 同时更新拖延/守时画像
        elif body.status == "pending":
            butler.memory.reopen_task(task_id)
        elif body.status is not None:
            raise HTTPException(status_code=400, detail="status 只能是 done / pending")
        return {"ok": True}

    @app.delete("/tasks/{task_id}")
    def delete_task(task_id: int) -> dict:
        butler.memory.delete_task(task_id)
        return {"ok": True}

    # ── 前端静态页（必须最后挂载，否则 "/" 会遮蔽上面所有 API 路由）──
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    print(f"启动 API 服务：http://{host}:{port}  (文档 /docs)")
    uvicorn.run(create_app(), host=host, port=port)
