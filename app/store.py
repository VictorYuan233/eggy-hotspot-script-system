"""SQLite 存储。

- snapshots 表只允许插入：用触发器禁止 UPDATE / DELETE，保证证据不可篡改。
- 其他对象按 JSON 存储，每次修改脚本都写入新版本，不覆盖旧版本。
- audit_log 记录全流程操作。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .models import AuditEntry, Snapshot, to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scripts (
    id TEXT PRIMARY KEY,          -- 形如 E01-douyin-v2，每个版本一行
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    # ---- 快照（只增不改） ----
    def add_snapshot(self, snap: Snapshot) -> bool:
        """返回 True 表示新写入；重复 ID 视为同一条，忽略。"""
        try:
            self.db.execute("INSERT INTO snapshots VALUES (?, ?)",
                            (snap.id, json.dumps(to_dict(snap), ensure_ascii=False)))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def snapshots(self) -> list[Snapshot]:
        rows = self.db.execute("SELECT payload FROM snapshots ORDER BY id").fetchall()
        return [Snapshot(**json.loads(r[0])) for r in rows]

    # ---- 通用 JSON 对象 ----
    def put(self, table: str, obj_id: str, payload: Any, **extra: str) -> None:
        cols = ["id", "payload", *extra.keys()]
        vals = [obj_id, json.dumps(to_dict(payload), ensure_ascii=False), *extra.values()]
        placeholders = ",".join("?" * len(cols))
        self.db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
        self.db.commit()

    def all(self, table: str) -> list[dict]:
        rows = self.db.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- 审计日志 ----
    def log(self, entry: AuditEntry) -> None:
        self.db.execute("INSERT INTO audit_log (at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                        (entry.at, entry.actor, entry.action, entry.target, entry.detail))
        self.db.commit()

    def audit(self) -> list[dict]:
        rows = self.db.execute("SELECT at, actor, action, target, detail FROM audit_log ORDER BY seq").fetchall()
        return [dict(zip(["at", "actor", "action", "target", "detail"], r)) for r in rows]

    def bulk_log(self, entries: Iterable[AuditEntry]) -> None:
        for e in entries:
            self.log(e)
