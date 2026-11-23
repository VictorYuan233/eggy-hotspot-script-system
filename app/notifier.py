"""推送：新候选选题、快速通道草稿就绪、超时告警推到飞书群。

使用飞书自定义机器人 webhook（环境变量 EGGY_FEISHU_WEBHOOK）。没有配置时只写审计日志，
不会假装已发送。
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from typing import Callable

from .models import AuditEntry
from .store import Store


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


class Notifier:
    def __init__(self, store: Store, webhook: str | None = None,
                 post: Callable[[str, dict], None] = _post):
        self.store = store
        self.webhook = webhook if webhook is not None else os.environ.get("EGGY_FEISHU_WEBHOOK", "")
        self.post = post

    def send(self, text: str, target: str, now: datetime) -> bool:
        if not self.webhook:
            self.store.log(AuditEntry(now.isoformat(), "system", "notify_skipped", target,
                                      f"未配置飞书 webhook，未发送：{text}"))
            return False
        try:
            self.post(self.webhook, {"msg_type": "text", "content": {"text": text}})
            self.store.log(AuditEntry(now.isoformat(), "system", "notify_feishu", target, text))
            return True
        except Exception as exc:
            self.store.log(AuditEntry(now.isoformat(), "system", "notify_failed", target, str(exc)[:200]))
            return False
