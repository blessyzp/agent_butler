"""资源感知监控器 —— 实时采样 VRAM/RAM/CPU，排除管家自身占用，评估压力。

调度器据此决定用本地大模型 / 本地小模型 / 云端。通用逻辑：不认游戏也不认
浏览器，只看"除管家外其他进程还剩多少资源"。
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass

import psutil

from .config import Config, get_config

# 管家自身相关进程名（这些占用不计入"其他进程"）
_BUTLER_PROC_NAMES = {"ollama.exe", "ollama", "python.exe", "python", "pythonw.exe"}


@dataclass
class ResourceSnapshot:
    vram_total_mb: int
    vram_used_by_others_mb: int
    vram_available_mb: int
    ram_total_gb: float
    ram_used_by_others_gb: float
    ram_available_gb: float
    cpu_percent: float
    pressure_level: str        # low / medium / high / critical
    gpu_present: bool


class ResourceMonitor:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self.poll_interval = self.cfg.get("resource.poll_interval_seconds", 10)
        self._snapshot: ResourceSnapshot | None = None
        self._lock = threading.RLock()  # 可重入：get() 持锁时 _update() 会再次加锁
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── 生命周期 ──
    def start(self) -> None:
        # 先同步采一次，保证 get() 立即有值
        self._update()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get(self) -> ResourceSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._update()
            return self._snapshot  # type: ignore[return-value]

    # ── 采样循环 ──
    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self._update()

    def _update(self) -> None:
        snap = self._collect()
        with self._lock:
            self._snapshot = snap

    def _collect(self) -> ResourceSnapshot:
        vram_total, vram_others, gpu_present = self._vram_usage()
        ram_total = psutil.virtual_memory().total / 1024**3
        ram_others = self._ram_used_by_others()
        cpu = psutil.cpu_percent(interval=0.3)

        vram_available = max(0, vram_total - vram_others)
        ram_available = max(0.0, ram_total - ram_others)
        pressure = self._pressure(vram_available, ram_available, cpu, gpu_present)

        return ResourceSnapshot(
            vram_total_mb=vram_total,
            vram_used_by_others_mb=vram_others,
            vram_available_mb=vram_available,
            ram_total_gb=round(ram_total, 1),
            ram_used_by_others_gb=round(ram_others, 1),
            ram_available_gb=round(ram_available, 1),
            cpu_percent=cpu,
            pressure_level=pressure,
            gpu_present=gpu_present,
        )

    # ── GPU 显存 ──
    # 优先按进程排除管家自身；当驱动不支持按进程查询（返回 [N/A]，
    # 常见于 GeForce 卡）时，回退到整卡 memory.used 作为"其他占用"近似。
    def _vram_usage(self) -> tuple[int, int, bool]:
        try:
            gpu_out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=memory.total,memory.used,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if gpu_out.returncode != 0:
                return 0, 0, False
            first = gpu_out.stdout.strip().splitlines()[0]
            total, used, free = (int(x.strip()) for x in first.split(","))

            # 尝试按进程排除自身
            apps_out = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            butler_pids = self._butler_pids()
            others = 0
            per_proc_ok = False
            for line in apps_out.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) != 2:
                    continue
                try:
                    pid, mem = int(parts[0].strip()), int(parts[1].strip())
                except ValueError:
                    continue  # "[N/A]" 等非数值
                per_proc_ok = True
                if pid not in butler_pids:
                    others += mem

            if per_proc_ok:
                # 按进程可用：其他占用 + 固定开销，剩余含我方已用（视为可回收）
                base_overhead = 500
                return total, others + base_overhead, True
            # 回退：整卡已用作为"其他占用"，available 即等于 memory.free
            return total, used, True
        except (subprocess.SubprocessError, FileNotFoundError, OSError, ValueError):
            return 0, 0, False

    def _ram_used_by_others(self) -> float:
        butler_pids = self._butler_pids()
        others_bytes = 0
        for proc in psutil.process_iter(["pid", "memory_info"]):
            try:
                if proc.pid not in butler_pids:
                    mi = proc.info["memory_info"]
                    if mi:
                        others_bytes += mi.rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return others_bytes / 1024**3

    def _butler_pids(self) -> set[int]:
        pids: set[int] = set()
        try:
            current = psutil.Process()
            pids.add(current.pid)
            for child in current.children(recursive=True):
                pids.add(child.pid)
            parent = current.parent()
            if parent:
                pids.add(parent.pid)
        except psutil.Error:
            pass
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").lower() in _BUTLER_PROC_NAMES:
                    pids.add(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    # ── 综合压力评级（取最紧张维度）──
    def _pressure(self, vram_mb: int, ram_gb: float,
                  cpu: float, gpu_present: bool) -> str:
        r = self.cfg
        ram_min = r.get("resource.ram_min_gb", 2.0)
        cpu_max = r.get("resource.cpu_max_percent", 85)

        # 无 GPU：本地大模型无从谈起，直接看是否还能跑小模型（CPU）
        if not gpu_present:
            return "high" if ram_gb >= ram_min else "critical"

        vram_large = r.get("resource.vram_large_min", 9500)
        vram_small = r.get("resource.vram_small_min", 5000)
        vram_tiny = r.get("resource.vram_tiny_min", 2000)

        if vram_mb >= vram_large and ram_gb >= ram_min and cpu < cpu_max:
            return "low"
        if vram_mb >= vram_small and ram_gb >= ram_min:
            return "medium"
        if vram_mb >= vram_tiny:
            return "high"
        return "critical"


def format_snapshot(s: ResourceSnapshot) -> str:
    gpu = (f"VRAM 可用 {s.vram_available_mb}MB / {s.vram_total_mb}MB"
           if s.gpu_present else "未检测到 NVIDIA GPU")
    return (f"{gpu}\n"
            f"内存 可用 {s.ram_available_gb}GB / {s.ram_total_gb}GB\n"
            f"CPU {s.cpu_percent:.0f}%  |  压力等级: {s.pressure_level}")
