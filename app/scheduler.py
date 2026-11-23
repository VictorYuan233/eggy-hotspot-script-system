"""定时采集：按数据源授权清单里配置的频率轮询公开 RSS，并跑完自动流水线。

    python -m app.scheduler --once        # 抓一轮
    python -m app.scheduler               # 常驻，按配置的间隔轮询
"""
from __future__ import annotations

import argparse
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from . import config
from .notifier import Notifier
from .pipeline import Pipeline

CST = timezone(timedelta(hours=8))


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "eggy-hotspot-bot/1.0"})
    return urllib.request.urlopen(req, timeout=15).read()


def parse_rss(xml: bytes | str, feed_name: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        pub = node.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).astimezone(CST).isoformat() if pub else ""
        except (TypeError, ValueError):
            published = ""
        digits = "".join(ch for ch in link.rsplit("/", 1)[-1] if ch.isdigit())
        items.append({
            "id": "C" + digits if digits else f"RSS{abs(hash(link)) % 10**8}",
            "source_id": "public_rss", "title": (node.findtext("title") or "").strip(), "url": link,
            "publisher": feed_name, "platform": "rss", "published_at": published,
            "summary": (node.findtext("description") or "").strip()[:200],
            "heat": 0, "heat_series": [], "heat_simulated": False,
        })
    return items


def run_once(pipe: Pipeline, now: datetime, fetch: Callable[[str], bytes] = http_get,
             notifier: Notifier | None = None) -> dict:
    feeds = next(s for s in config.sources()["sources"] if s["id"] == "public_rss").get("feeds", [])
    known_urls = {s.url for s in pipe.store.snapshots()}
    raw, errors = [], []
    for feed in feeds:
        try:
            raw += parse_rss(fetch(feed["url"]), feed["name"])
        except Exception as exc:
            errors.append(f"{feed['name']}：{exc}")
    fresh = [r for r in raw if r["url"] not in known_urls]
    round_id = "rss-" + now.strftime("%Y-%m-%dT%H%M")
    before = set(pipe.events)
    pipe.run_intake(fresh, now, round_id)
    new_events = [pipe.events[i] for i in pipe.events if i not in before]
    if notifier:
        for ev in new_events:
            if ev.status.value == "to_select":
                notifier.send(f"新候选选题 {ev.id}：{ev.title}（总分 {ev.score.get('total')}）", ev.id, now)
    return {"round_id": round_id, "fetched": len(raw), "new": len(fresh), "errors": errors,
            "new_events": [e.id for e in new_events]}


def main() -> None:
    from .llm import AnthropicLLM
    from .store import Store
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db", default="eggy.db")
    args = ap.parse_args()
    store = Store(args.db)
    pipe, notifier = Pipeline(store, AnthropicLLM()), Notifier(store)
    interval = min(f.get("interval_minutes", 15) for s in config.sources()["sources"]
                   for f in s.get("feeds", [])) if any(s.get("feeds") for s in config.sources()["sources"]) else 15
    while True:
        print(run_once(pipe, datetime.now(CST), notifier=notifier))
        if args.once:
            break
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
