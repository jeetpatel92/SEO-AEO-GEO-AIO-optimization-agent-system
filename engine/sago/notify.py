"""Tells the owner things: run summaries, rollbacks, halts, the weekly digest.

Backends: Slack incoming webhook (SLACK_WEBHOOK_URL) and/or Telegram
(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). Every notification is also written to
the events table, so nothing is lost when no backend is set. A failed
notification never breaks a run.
"""
from __future__ import annotations

import json
import os
import urllib.request

from .io.http import _CTX


def _post_json(url: str, payload: dict, timeout: int = 15) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        r.read()


class Notifier:
    def __init__(self, store, env: dict | None = None, quiet: bool = False):
        env = env if env is not None else os.environ
        self.store = store
        self.slack = env.get("SLACK_WEBHOOK_URL", "")
        self.tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = env.get("TELEGRAM_CHAT_ID", "")
        self.quiet = quiet
        self.sent: list[dict] = []

    def send(self, title: str, body: str = "", level: str = "info") -> None:
        icon = {"critical": "🛑", "warn": "⚠️"}.get(level, "🔔")
        text = f"{icon} {title}" + (f"\n{body}" if body else "")
        self.sent.append({"title": title, "body": body, "level": level})
        self.store.event("critical" if level == "critical" else "warn" if level == "warn" else "info",
                         "notify", title, {"body": body})
        if not (self.slack or (self.tg_token and self.tg_chat)):
            if not self.quiet:
                print(text, flush=True)
            return
        if self.slack:
            try:
                _post_json(self.slack, {"text": text})
            except Exception as e:  # noqa: BLE001
                self.store.event("warn", "notify", f"Slack send failed: {e}")
        if self.tg_token and self.tg_chat:
            try:
                _post_json(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                           {"chat_id": self.tg_chat, "text": text, "disable_web_page_preview": True})
            except Exception as e:  # noqa: BLE001
                self.store.event("warn", "notify", f"Telegram send failed: {e}")
