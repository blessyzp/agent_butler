#!/usr/bin/env python
"""跑全部测试套件。

    python tests/run_all.py           # 全部（含真实模型对话，慢）
    python tests/run_all.py --fast    # 跳过需要 Ollama 的用例

每个套件在独立子进程里运行：它们各自持有 config 单例和 SQLite 连接，
同进程串跑会互相污染（而且同进程两个 Chroma 客户端会 segfault）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (文件名, 说明, 是否需要 Ollama 真实模型)
SUITES = [
    ("test_migrate.py",   "schema v1→v2 迁移安全性", False),
    ("test_backup.py",    "WAL + 备份/恢复完整性", False),
    ("test_reminder2.py", "提醒退避 / 配额 / 拖延画像", False),
    ("test_smoke.py",     "API 与前端 30 项冒烟", False),
    ("test_due.py",       "due_at 日期修复（真实对话）", True),
]


def main() -> None:
    fast = "--fast" in sys.argv
    results: list[tuple[str, bool, str]] = []

    for fname, desc, needs_model in SUITES:
        if fast and needs_model:
            print(f"\n{'#' * 60}\n# 跳过 {fname} —— {desc}（--fast）\n{'#' * 60}")
            results.append((desc, True, "skipped"))
            continue
        print(f"\n{'#' * 60}\n# {fname} —— {desc}\n{'#' * 60}", flush=True)
        r = subprocess.run([sys.executable, "-u", str(HERE / fname)],
                           env={**__import__("os").environ,
                                "PYTHONIOENCODING": "utf-8"})
        results.append((desc, r.returncode == 0, "ok" if r.returncode == 0 else "FAILED"))

    print(f"\n{'=' * 60}\n汇总\n{'=' * 60}")
    for desc, ok, note in results:
        mark = "跳过" if note == "skipped" else ("通过" if ok else "失败")
        print(f"  [{mark}]  {desc}")
    bad = [d for d, ok, _ in results if not ok]
    print(f"\n{'全部通过' if not bad else f'{len(bad)} 个套件失败'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
