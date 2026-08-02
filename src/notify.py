"""通知输出 —— Bark / Telegram / 控制台，多通道尽力送达。

隐私：敏感提醒可只推"有重要事项，请打开管家查看"，正文不出现在锁屏。
（由 reminder 层决定是否脱敏，本层只负责投递。）
"""
from __future__ import annotations

import urllib.parse

import requests

from .config import Config, get_config


class Notifier:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()

    def send(self, title: str, body: str) -> bool:
        """向所有已配置通道投递，任一成功即返回 True；否则回落到控制台。"""
        ok = False
        if self._bark_configured():
            ok = self._send_bark(title, body) or ok
        if self._telegram_configured():
            ok = self._send_telegram(title, body) or ok
        if not ok:
            print(f"\n🔔 [{title}] {body}\n")  # 控制台兜底
        return ok

    # ── Bark (iOS) ──
    def _bark_configured(self) -> bool:
        return bool(self.cfg.secret("BARK_KEY"))

    def _send_bark(self, title: str, body: str) -> bool:
        try:
            server = self.cfg.secret("BARK_SERVER", "https://api.day.app")
            key = self.cfg.secret("BARK_KEY")
            url = f"{server}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
            r = requests.get(url, timeout=8)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ── Telegram ──
    def _telegram_configured(self) -> bool:
        return bool(self.cfg.secret("TELEGRAM_BOT_TOKEN")
                    and self.cfg.secret("TELEGRAM_CHAT_ID"))

    def _send_telegram(self, title: str, body: str) -> bool:
        try:
            token = self.cfg.secret("TELEGRAM_BOT_TOKEN")
            chat_id = self.cfg.secret("TELEGRAM_CHAT_ID")
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"*{title}*\n{body}",
                      "parse_mode": "Markdown"},
                timeout=8,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False
