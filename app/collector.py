"""① 热点采集：只从数据源授权清单里登记、启用且未过期的数据源取数据。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable

from . import config
from .models import AuditEntry, Snapshot
from .store import Store


class SourceNotAuthorized(PermissionError):
    pass


class SourceRegistry:
    def __init__(self, registry: dict | None = None):
        self.sources = {s["id"]: s for s in (registry or config.sources())["sources"]}

    def check(self, source_id: str, now: datetime) -> dict:
        src = self.sources.get(source_id)
        if src is None:
            raise SourceNotAuthorized(f"数据源 {source_id} 不在授权清单里")
        if src["status"] != "active":
            raise SourceNotAuthorized(f"数据源 {source_id} 未启用：{src['auth']}")
        if src.get("valid_until") and now.date().isoformat() > src["valid_until"]:
            raise SourceNotAuthorized(f"数据源 {source_id} 的授权已于 {src['valid_until']} 到期")
        return src


class Collector:
    def __init__(self, store: Store, registry: SourceRegistry | None = None):
        self.store = store
        self.registry = registry or SourceRegistry()

    def ingest(self, raw_items: list[dict], now: datetime) -> list[Snapshot]:
        """写入一批原始条目。未授权来源的条目被跳过并记日志。"""
        saved = []
        for item in raw_items:
            try:
                self.registry.check(item["source_id"], now)
            except SourceNotAuthorized as exc:
                self.store.log(AuditEntry(now.isoformat(), "system", "skip_unauthorized",
                                          item.get("id", "?"), str(exc)))
                continue
            snap = Snapshot(
                id=item["id"], source_id=item["source_id"], title=item["title"],
                url=item.get("url", ""), publisher=item.get("publisher", ""),
                platform=item.get("platform", ""), published_at=item["published_at"],
                fetched_at=item.get("fetched_at", now.isoformat()), summary=item.get("summary", ""),
                heat=item.get("heat", 0), heat_series=item.get("heat_series", []),
                heat_simulated=item.get("heat_simulated", True),
            )
            if self.store.add_snapshot(snap):
                saved.append(snap)
                self.store.log(AuditEntry(now.isoformat(), "system", "snapshot_saved", snap.id,
                                          f"{snap.source_id} | {snap.url or '无链接'}"))
        return saved

    def submit_link(self, snap_id: str, title: str, url: str, summary: str, submitted_by: str,
                    now: datetime) -> list[Snapshot]:
        """运营手动提交链接（没有合规接口的平台走这条路）。"""
        item = {"id": snap_id, "source_id": "manual_submit", "title": title, "url": url,
                "publisher": f"运营提交：{submitted_by}", "platform": "manual",
                "published_at": now.isoformat(), "summary": summary}
        return self.ingest([item], now)

    def pull_rss(self, feed_url: str, fetch: Callable[[str], str], now: datetime,
                 id_prefix: str = "RSS") -> list[Snapshot]:
        """从公开 RSS 拉取。fetch 由调用方注入，便于测试和统一走代理。"""
        root = ET.fromstring(fetch(feed_url))
        items = []
        for i, node in enumerate(root.iter("item")):
            items.append({
                "id": f"{id_prefix}{i:03d}", "source_id": "public_rss",
                "title": (node.findtext("title") or "").strip(),
                "url": (node.findtext("link") or "").strip(),
                "publisher": feed_url, "platform": "rss",
                "published_at": now.isoformat(),
                "summary": (node.findtext("description") or "").strip()[:200],
            })
        return self.ingest(items, now)
