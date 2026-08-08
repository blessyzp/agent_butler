"""记忆系统 —— 真相源(加密SQLite) + 派生向量索引(ChromaDB)。

数据安全设计：
  • 结构化真相：SQLite，敏感字段用 Cipher 加密后存储
  • 情景记忆真相：episodes 表（加密文本）—— 向量任何时候可从此重建
  • 向量索引：Chroma 集合名 = episodes__<embed_model_id>，按嵌入模型隔离
      → 换嵌入模型 = 新集合名，自动从真相源重嵌入，旧集合留作回滚
  • Chroma 缺失或嵌入后端离线 → 降级为"按时间召回"，功能不中断

隐私：因内容加密，SQL 层无法全文检索；语义检索走向量，降级走时间召回。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .backup import BackupManager
from .config import Config, get_config
from .crypto import Cipher
from .registry import ModelRegistry, get_registry
from . import versioning


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    def __init__(self,
                 cfg: Config | None = None,
                 cipher: Cipher | None = None,
                 registry: ModelRegistry | None = None):
        self.cfg = cfg or get_config()
        self.cipher = cipher or Cipher.instance()
        self.registry = registry or get_registry()
        self.backup = BackupManager(self.cfg)

        # ── SQLite 真相源 ──
        db_path = self.cfg.get("paths.memory_db")
        # timeout：提醒引擎的后台线程与 API 请求线程会并发写同一个库，
        # 遇到锁时等待而不是立刻抛 "database is locked"。
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        # WAL：读写不互相阻塞，显著降低并发写冲突。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        versioning.ensure_schema(self.conn)

        # ── 向量索引（可选）──
        self.vector_enabled = False
        self._collection = None
        self._client = None
        self._init_vector()

    # ════════════════════════════════════════════════════
    #  向量索引初始化 + 嵌入迁移
    # ════════════════════════════════════════════════════
    def _collection_name(self) -> str:
        # 集合名绑定嵌入模型 ID → 换模型自动切新集合
        mid = self.registry.embed_model_id().replace(":", "_").replace(".", "_")
        return f"episodes__{mid}"

    def _init_vector(self) -> None:
        # 显式关闭开关：向量只是可重建的缓存，后端一旦不稳定（例如 chromadb
        # 的 wheel 与本机 numpy ABI 不匹配会直接段错误，Python 层 try/except
        # 拦不住）必须能一键降级，不能让缓存层拖垮整个管家。
        if not self.cfg.get("memory.vector_enabled", True):
            print("ⓘ 配置已关闭向量检索（memory.vector_enabled=false），使用时间召回。")
            return

        try:
            import chromadb
        except ImportError:
            print("ⓘ 未安装 chromadb，语义检索降级为时间召回。")
            return

        try:
            client = chromadb.PersistentClient(path=self.cfg.get("paths.vector_dir"))
            self._client = client
            self._collection = client.get_or_create_collection(
                self._collection_name(),
                metadata={"embed_model": self.registry.embed_model_id()},
            )
            self.vector_enabled = True
        except Exception as e:
            print(f"ⓘ 向量库初始化失败（{e}），降级为时间召回。")
            return

        # 嵌入模型变更检测 → 触发重嵌入迁移
        self._migrate_embeddings_if_needed()

    def _migrate_embeddings_if_needed(self) -> None:
        current_id = self.registry.embed_model_id()
        current_dim = self.registry.embed_dim()

        changed = versioning.embedding_changed(self.conn, current_id, current_dim)
        # 新集合为空但真相源有数据 → 也需要（重新）填充
        need_fill = (self._collection.count() == 0
                     and self._episode_count() > 0)

        if not (changed or need_fill):
            versioning.record_embedding_model(self.conn, current_id, current_dim)
            return

        print(f"⟳ 检测到需要重建向量索引（模型={current_id}）。先备份再重嵌入…")
        self.backup.snapshot(reason="reembed")
        try:
            self._reembed_all()
            versioning.record_embedding_model(self.conn, current_id, current_dim)
            print("✓ 向量索引重建完成，旧集合已保留用于回滚。")
        except Exception as e:
            print(f"✗ 重嵌入失败（{e}）。真相源未受影响，语义检索暂降级。")

    def _reembed_all(self) -> None:
        """从 SQLite 真相源逐条重算向量，写入当前集合。"""
        rows = self.conn.execute(
            "SELECT id, detail_enc, created_at FROM events "
            "WHERE kind = 'episode' ORDER BY id"
        ).fetchall()
        batch_texts, batch_ids, batch_meta = [], [], []
        for row in rows:
            text = self.cipher.decrypt(row["detail_enc"])
            batch_texts.append(text)
            batch_ids.append(f"ep_{row['id']}")
            batch_meta.append({"created_at": row["created_at"]})

        if not batch_texts:
            return

        embeddings = self.registry.embed(batch_texts)
        # 校验：条数必须与真相源一致，否则视为失败
        if len(embeddings) != len(batch_texts):
            raise RuntimeError("重嵌入条数与真相源不一致，中止以防数据不一致")

        # 写入前清空当前集合（同名集合重建，旧模型集合另名保留）
        try:
            self._client.delete_collection(self._collection_name())
        except Exception:
            pass
        self._collection = self._client.get_or_create_collection(
            self._collection_name(),
            metadata={"embed_model": self.registry.embed_model_id()},
        )
        # Chroma 存密文文档，避免明文落盘
        self._collection.add(
            ids=batch_ids,
            embeddings=embeddings,
            documents=[self.cipher.encrypt(t) for t in batch_texts],
            metadatas=batch_meta,
        )

    def _episode_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'episode'"
        ).fetchone()[0]

    # ════════════════════════════════════════════════════
    #  情景记忆（对话片段）
    # ════════════════════════════════════════════════════
    def add_episode(self, text: str, meta: dict | None = None) -> int:
        """写入真相源 + 向量索引（后者尽力而为）。"""
        created = _now_iso()
        cur = self.conn.execute(
            "INSERT INTO events(kind, detail_enc, created_at) VALUES('episode', ?, ?)",
            (self.cipher.encrypt(text), created),
        )
        self.conn.commit()
        ep_id = cur.lastrowid

        if self.vector_enabled:
            try:
                emb = self.registry.embed([text])[0]
                self._collection.add(
                    ids=[f"ep_{ep_id}"],
                    embeddings=[emb],
                    documents=[self.cipher.encrypt(text)],
                    metadatas=[{"created_at": created, **(meta or {})}],
                )
            except Exception:
                pass  # 向量写入失败不影响真相源
        return ep_id

    def retrieve(self, query: str, k: int = 5) -> list[str]:
        """语义召回；不可用时降级为最近 k 条。"""
        if self.vector_enabled:
            try:
                q_emb = self.registry.embed([query])[0]
                res = self._collection.query(query_embeddings=[q_emb], n_results=k)
                docs = res.get("documents", [[]])[0]
                return [self.cipher.decrypt(d) for d in docs]
            except Exception:
                pass
        return self._recent_episodes(k)

    def _recent_episodes(self, k: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT detail_enc FROM events WHERE kind='episode' "
            "ORDER BY id DESC LIMIT ?", (k,),
        ).fetchall()
        return [self.cipher.decrypt(r["detail_enc"]) for r in rows]

    def recent_episodes(self, k: int = 30) -> list[dict]:
        """最近 k 条情景记忆，按时间正序（含时间戳）—— 供前端恢复对话历史。"""
        rows = self.conn.execute(
            "SELECT id, detail_enc, created_at FROM events WHERE kind='episode' "
            "ORDER BY id DESC LIMIT ?", (k,),
        ).fetchall()
        return [
            {"id": r["id"], "text": self.cipher.decrypt(r["detail_enc"]),
             "created_at": r["created_at"]}
            for r in reversed(rows)
        ]

    # ════════════════════════════════════════════════════
    #  任务
    # ════════════════════════════════════════════════════
    def add_task(self, content: str, task_type: str | None = None,
                 due_at: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks(content_enc, task_type, due_at, created_at) "
            "VALUES(?, ?, ?, ?)",
            (self.cipher.encrypt(content), task_type, due_at, _now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_tasks(self, status: str = "pending") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY due_at IS NULL, due_at",
            (status,),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def due_tasks(self, before_iso: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status='pending' AND due_at IS NOT NULL "
            "AND due_at <= ? ORDER BY due_at", (before_iso,),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def complete_task(self, task_id: int) -> dict | None:
        """标记完成，返回完成前的任务行（供调用方判断是否按时，更新画像）。"""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        self.conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (_now_iso(), task_id),
        )
        self.conn.commit()
        return self._task_row(row) if row else None

    def reopen_task(self, task_id: int) -> None:
        """撤销完成，回到 pending。"""
        self.conn.execute(
            "UPDATE tasks SET status='pending', completed_at=NULL WHERE id=?",
            (task_id,),
        )
        self.conn.commit()

    def update_task_content(self, task_id: int, content: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET content_enc=? WHERE id=?",
            (self.cipher.encrypt(content), task_id),
        )
        self.conn.commit()

    def delete_task(self, task_id: int) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.conn.commit()

    def bump_reminded(self, task_id: int) -> None:
        """记一次提醒。必须同时写 last_reminded_at，逾期退避依赖它。"""
        self.conn.execute(
            "UPDATE tasks SET reminded_count = reminded_count + 1, "
            "last_reminded_at = ? WHERE id=?",
            (_now_iso(), task_id),
        )
        self.conn.commit()

    def count_events_since(self, kind: str, since: datetime) -> int:
        """某类事件自某时刻起的条数。用于跨重启的每日提醒配额统计。

        入参收 datetime 而不是字符串：库里存的是 UTC（_now_iso），调用方传本地
        时间的话字符串比较会静默出错，这里统一归一化。
        """
        since_utc = since.astimezone(timezone.utc).isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? AND created_at >= ?",
            (kind, since_utc),
        ).fetchone()[0]

    def _task_row(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "content": self.cipher.decrypt(r["content_enc"]),
            "task_type": r["task_type"],
            "status": r["status"],
            "due_at": r["due_at"],
            "created_at": r["created_at"],
            "reminded_count": r["reminded_count"],
            "last_reminded_at": r["last_reminded_at"],
        }

    # ════════════════════════════════════════════════════
    #  偏好 / 事件
    # ════════════════════════════════════════════════════
    def set_preference(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO preferences(key, value_enc, updated_at) VALUES(?, ?, ?)",
            (key, self.cipher.encrypt(value), _now_iso()),
        )
        self.conn.commit()

    def get_preferences(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT key, value_enc, MAX(updated_at) FROM preferences GROUP BY key"
        ).fetchall()
        return {r["key"]: self.cipher.decrypt(r["value_enc"]) for r in rows}

    def log_event(self, kind: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(kind, detail_enc, created_at) VALUES(?, ?, ?)",
            (kind, self.cipher.encrypt(detail) if detail else None, _now_iso()),
        )
        self.conn.commit()

    def last_activity_iso(self) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(created_at) FROM events WHERE kind IN ('episode','user_msg')"
        ).fetchone()
        return row[0] if row and row[0] else None

    def close(self) -> None:
        self.conn.close()
        # PersistentClient 内部持有自己的 sqlite 连接（chroma.sqlite3），不主动
        # stop() 的话文件锁不释放——备份/恢复时 shutil.rmtree 向量目录会因
        # PermissionError 失败（Windows 下文件被占用不能删）。
        if self._client is not None:
            self._client._system.stop()
