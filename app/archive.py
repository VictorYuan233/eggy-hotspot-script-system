"""证据存档：采集时把原文页面保存下来，并记录指纹，证明“当时看到的就是这个”。

- 原始 HTML 存到本地 data/archive/（已加入 .gitignore，不公开转载原文）。
- 存档记录（时间、字节数、SHA-256、文件路径）写入数据库，可随时比对原文是否被改动或删除。
- 截图需要无头浏览器，接口预留为 screenshot 回调，生产环境接入。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import DATA_DIR
from .models import AuditEntry, Snapshot
from .store import Store

ARCHIVE_DIR = DATA_DIR / "archive"


def wayback_lookup(url: str) -> str:
    """互联网档案馆的查询地址，供人工交叉查看历史版本。"""
    return f"https://web.archive.org/web/*/{url}" if url else ""


def archive(snap: Snapshot, fetch: Callable[[str], bytes], store: Store, now: datetime,
            screenshot: Callable[[str], bytes] | None = None, folder: Path = ARCHIVE_DIR) -> dict:
    record = {"snapshot_id": snap.id, "url": snap.url, "archived_at": now.isoformat(),
              "status": "skipped", "wayback": wayback_lookup(snap.url)}
    if not snap.url:
        record["reason"] = "没有链接"
    else:
        try:
            body = fetch(snap.url)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{snap.id}.html"
            path.write_bytes(body)
            record.update(status="ok", bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), file=str(path))
            if screenshot:
                shot = folder / f"{snap.id}.png"
                shot.write_bytes(screenshot(snap.url))
                record["screenshot"] = str(shot)
        except Exception as exc:  # 抓取失败也要留下记录，便于补存
            record.update(status="failed", reason=str(exc)[:200])
    store.db.execute("CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    store.db.execute("INSERT OR REPLACE INTO archives VALUES (?, ?)",
                     (snap.id, json.dumps(record, ensure_ascii=False)))
    store.db.commit()
    store.log(AuditEntry(now.isoformat(), "system", "archive", snap.id, record["status"]))
    return record


def verify_archive(record: dict, fetch: Callable[[str], bytes]) -> str:
    """重新抓取原文并比对指纹：same / changed / gone。"""
    try:
        body = fetch(record["url"])
    except Exception:
        return "gone"
    return "same" if hashlib.sha256(body).hexdigest() == record.get("sha256") else "changed"
