#!/usr/bin/env python
"""电子管家 CLI 入口。

用法：
    python run.py            # 进入对话
    python run.py serve      # 启动 HTTP API（供前端接入，localhost:8000）
    python run.py status     # 打印一次状态后退出
    python run.py doctor     # 环境自检（不加载模型）
"""
from __future__ import annotations

import sys

# Windows 控制台默认 GBK，重配为 UTF-8 以正常输出 ✓ / emoji / 中文框线
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _doctor() -> None:
    """环境自检：依赖、配置、后端可用性。不触发加密/模型加载失败即通过。"""
    print("── 环境自检 ──")
    # 依赖
    for mod in ("requests", "psutil", "apscheduler", "yaml", "cryptography"):
        try:
            __import__(mod)
            print(f"  ✓ {mod}")
        except ImportError:
            print(f"  ✗ {mod}  (pip install -r requirements.txt)")
    try:
        import chromadb  # noqa
        print("  ✓ chromadb（语义检索可用）")
    except ImportError:
        print("  ⚠ chromadb 缺失（将降级为时间召回，可选装）")

    # 配置
    from src.config import get_config
    cfg = get_config()
    print(f"  ✓ 配置加载：数据目录 {cfg.get('paths.data_dir')}")
    print(f"  {'✓' if cfg.has_deepseek() else '⚠'} DeepSeek 云端兜底"
          f"{'已配置' if cfg.has_deepseek() else '未配置（游戏/高负载时无兜底）'}")

    # 资源
    from src.resource_monitor import ResourceMonitor, format_snapshot
    mon = ResourceMonitor(cfg)
    print("── 资源快照 ──")
    print(format_snapshot(mon.get()))

    # 后端
    from src.registry import get_registry
    print("── 模型后端可用性 ──")
    for role, ok in get_registry().availability().items():
        print(f"  {'✓' if ok else '✗'} {role}")


def _status() -> None:
    from src.butler import Butler
    b = Butler()
    print(b.status())
    b.memory.close()


def _repl() -> None:
    from src.butler import Butler
    print("正在启动电子管家…")
    b = Butler()
    b.start()
    print(b.status())
    print("\n输入对话即可。命令：/status 查看状态，/quit 退出。\n")
    try:
        while True:
            try:
                text = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/status":
                print(b.status())
                continue
            reply = b.chat(text)
            print(f"管家 > {reply}\n")
    finally:
        print("\n正在保存并退出…")
        b.shutdown()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "repl"
    if cmd == "doctor":
        _doctor()
    elif cmd == "status":
        _status()
    elif cmd == "serve":
        from src.api import serve
        serve()
    else:
        _repl()


if __name__ == "__main__":
    main()
