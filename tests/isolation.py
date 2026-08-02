"""测试隔离样板 —— 所有测试必须先调用 setup()，绝不允许碰真实的 D:/butler/data。

为什么需要它：`Butler()` / `create_app()` / `Memory()` 都会走 config 单例去拿
paths.*，并触发 Cipher.instance()。不隔离就会：
  1. 往真实库里写测试垃圾数据（曾经发生过）
  2. 在非交互 shell 里卡死在 getpass 主密码提示上

用法（必须在 import 任何 src.* 之前调用）：
    from isolation import setup, check, report
    data = setup("mytest")
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_tmp: Path | None = None
_fails: list[str] = []


def setup(prefix: str = "butler_test") -> Path:
    """建临时数据目录 + 重定向所有 paths.* + 占住 config 单例。返回数据目录。"""
    global _tmp
    sys.path.insert(0, str(ROOT))
    _tmp = Path(tempfile.mkdtemp(prefix=f"{prefix}_"))
    data = _tmp / "data"
    data.mkdir()

    raw = (ROOT / "config.yaml").read_text(encoding="utf-8")
    iso = re.sub(r'"D:/butler/data([^"]*)"',
                 lambda m: '"' + (data.as_posix() + m.group(1)) + '"', raw)
    # 硬校验：重定向没生效就绝不继续，否则会污染真实数据
    assert "D:/butler/data" not in iso, "路径重定向失败，拒绝运行测试"
    (_tmp / "config.yaml").write_text(iso, encoding="utf-8")

    os.environ["BUTLER_MASTER_PASSWORD"] = "test-throwaway-password"
    import src.config as C
    C._instance = C.Config(str(_tmp / "config.yaml"))
    assert C.get_config().get("paths.data_dir") == data.as_posix()
    return data


def check(name: str, cond: bool, extra: object = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{('  ' + str(extra)) if extra != '' else ''}", flush=True)
    if not cond:
        _fails.append(name)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n── {title} ──", flush=True)


def report() -> None:
    """打印汇总、清理临时目录，并以正确的退出码结束进程。"""
    print(f"\n{'=' * 54}")
    print("结果：全部通过" if not _fails
          else f"{len(_fails)} 项失败 -> {', '.join(_fails)}")
    print("=" * 54)
    if _tmp:
        shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(1 if _fails else 0)
