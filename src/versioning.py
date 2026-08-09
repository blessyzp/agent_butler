"""版本与迁移 —— 保证 schema 升级、模型更换前后数据不丢。

两类版本：
  1. SQLite 结构化 schema  → schema_version + 只增不删的迁移
  2. 向量嵌入模型          → meta 表记录当前 embed 模型/维度，变更触发重嵌入

meta 表：通用键值，记录 embed_model_id、embed_dim、schema_version 等。
"""
from __future__ import annotations

import sqlite3

# ── 当前结构化 schema 版本 ──
CURRENT_SCHEMA_VERSION = 3

# ── 迁移脚本：只增不删，按版本顺序执行 ──
# 每项 = (目标版本, SQL 语句列表)。新增字段/表放这里，绝不 DROP 用户数据。
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        # 任务表：content 字段加密存储（Cipher）
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content_enc TEXT NOT NULL,
            task_type   TEXT,
            status      TEXT DEFAULT 'pending',
            due_at      TEXT,
            created_at  TEXT NOT NULL,
            completed_at TEXT,
            reminded_count INTEGER DEFAULT 0
        )
        """,
        # 偏好/画像信号
        """
        CREATE TABLE IF NOT EXISTS preferences (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT NOT NULL,
            value_enc  TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        # 事件日志（沉默检测、提醒记账等）
        """
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            detail_enc TEXT,
            created_at TEXT NOT NULL
        )
        """,
    ]),
    # v2：记录上次提醒时刻，逾期追问才能退避。没有它就只能靠 reminded_count，
    # 无法判断"距离上次提醒过了多久"，会每个 tick 都重发（见 reminder.py）。
    (2, ["ALTER TABLE tasks ADD COLUMN last_reminded_at TEXT"]),
    # v3：优先级 + 重复任务。priority 数字越大越紧急（显示排序靠前），recurrence
    # 是 "daily"/"weekly"/"monthly" 或 null —— 完成后按此规则自动生成下一条。
    (3, [
        "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN recurrence TEXT",
    ]),
]


def _get_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_user_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute(f"PRAGMA user_version = {v}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """应用所有待执行迁移。幂等，可安全反复调用。"""
    current = _get_user_version(conn)
    for target, statements in MIGRATIONS:
        if current < target:
            for sql in statements:
                conn.execute(sql)
            _set_user_version(conn, target)
            current = target
    conn.commit()


# ── meta 键值访问 ──
def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# ── 嵌入模型兼容判定 ──
def embedding_changed(conn: sqlite3.Connection,
                      current_model_id: str,
                      current_dim: int | None) -> bool:
    """当前配置的嵌入模型是否与已记录的不同 → 需重嵌入。"""
    recorded_model = get_meta(conn, "embed_model_id")
    recorded_dim = get_meta(conn, "embed_dim")

    if recorded_model is None:
        return False  # 首次使用，无历史向量，无需迁移
    if recorded_model != current_model_id:
        return True
    if current_dim is not None and recorded_dim != str(current_dim):
        return True
    return False


def record_embedding_model(conn: sqlite3.Connection,
                           model_id: str, dim: int | None) -> None:
    set_meta(conn, "embed_model_id", model_id)
    if dim is not None:
        set_meta(conn, "embed_dim", str(dim))
