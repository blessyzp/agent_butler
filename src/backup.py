"""备份子系统 —— 任何迁移前先快照，出错可回滚。

策略：
  • 迁移/重嵌入前自动调用 snapshot()，时间戳 + 原因命名
  • 复制真相源（memory.db）、画像、向量目录
  • 保留最近 N 份，自动清理更旧的
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Config, get_config


class BackupManager:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self.data_dir = Path(self.cfg.get("paths.data_dir"))
        self.backup_root = self.data_dir / "backups"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.keep = self.cfg.get("backup.keep_count", 10)

    def snapshot(self, reason: str = "manual") -> Path:
        """创建一份快照，返回快照目录。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_reason = "".join(c for c in reason if c.isalnum() or c in "-_")
        dest = self.backup_root / f"{ts}_{safe_reason}"
        dest.mkdir(parents=True, exist_ok=True)

        db_path = Path(self.cfg.get("paths.memory_db"))
        # SQLite 处于 WAL 模式，最近的事务可能还只在 -wal 里。用官方 backup API
        # 做一致性快照，而不是裸复制 .db（那样会丢掉未 checkpoint 的数据）。
        if db_path.exists():
            self._backup_sqlite(db_path, dest / db_path.name)

        profile = Path(self.cfg.get("paths.profile_file"))
        if profile.exists():
            shutil.copy2(profile, dest / profile.name)

        settings_file = self.data_dir / "settings.json"
        if settings_file.exists():
            shutil.copy2(settings_file, dest / settings_file.name)

        # 向量目录（可能较大，整目录复制）
        vector_dir = Path(self.cfg.get("paths.vector_dir"))
        if vector_dir.exists():
            shutil.copytree(vector_dir, dest / vector_dir.name,
                            dirs_exist_ok=True)

        self._prune()
        print(f"✓ 已备份至 {dest}")
        return dest

    @staticmethod
    def _backup_sqlite(src: Path, dest: Path) -> None:
        """用 SQLite 官方 backup API 做一致性快照（含 WAL 中未落盘的事务）。"""
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    def list_snapshots(self) -> list[Path]:
        return sorted(
            (p for p in self.backup_root.iterdir() if p.is_dir()),
            reverse=True,
        )

    def restore(self, snapshot_dir: str | Path) -> None:
        """从指定快照恢复（覆盖当前数据）。恢复前会先备份当前状态。"""
        snap = Path(snapshot_dir)
        if not snap.exists():
            raise FileNotFoundError(f"快照不存在: {snap}")

        # 恢复前先给当前状态兜底备份（注意：这会打开连接并重建 -wal，
        # 所以 WAL 清理必须排在它之后，否则清了个空）
        self.snapshot(reason="pre_restore")

        # 清掉遗留的 WAL/SHM：否则 SQLite 会用旧 -wal 里的事务覆盖刚恢复的库
        db_path = Path(self.cfg.get("paths.memory_db"))
        for suffix in ("-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

        for item in snap.iterdir():
            target = self.data_dir / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        print(f"✓ 已从 {snap} 恢复")
        print("  ⚠ 恢复后请重启管家（进程内的旧连接仍指向被替换的文件）")

    def _prune(self) -> None:
        snaps = self.list_snapshots()
        for old in snaps[self.keep:]:
            shutil.rmtree(old, ignore_errors=True)
