"""V2 新增：多轮抓取、过期清理、来源补充、定时采集、证据存档、推送、回流建议、评测集、持久化。"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app import feedback
from app.archive import archive, verify_archive, wayback_lookup
from app.config import DATA_DIR
from app.llm import ReplayLLM
from app.models import EventStatus, ScriptStatus
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.scheduler import parse_rss, run_once
from app.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import eval as evalset  # noqa: E402

NOW = datetime.fromisoformat("2026-11-22T21:45:00+08:00")
R2 = json.loads((DATA_DIR / "crawl_rss_2026-11-23.json").read_text(encoding="utf-8"))
NOW2 = datetime.fromisoformat(R2["fetched_at"])


def seed():
    s = json.loads((DATA_DIR / "seed_hotspots.json").read_text(encoding="utf-8"))
    return [{**x, "fetched_at": s["collected_at"]} for x in s["snapshots"]]


def key(p, k):
    return next(e for e in p.events.values() if e.key == k)


@pytest.fixture
def two_rounds():
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(seed(), NOW, "manual")
    new = p.run_intake(R2["items"], NOW2, R2["round_id"])
    return p, new


def test_second_round_prefilters_and_only_builds_new_events(two_rounds):
    p, new = two_rounds
    assert len(p.store.snapshots()) == 18 + 30           # 全部存档
    assert {e.key for e in new} == {"C10736044", "C10736045", "C10736046", "C10736055"}
    assert any(a["action"] == "prefilter_skip" and "24" in a["detail"] for a in p.store.audit())
    assert sum(1 for e in p.events.values() if e.key == "R01") == 1   # 旧事件没有被重建


def test_unapproved_hotspot_expires_and_late_source_only_archived(two_rounds):
    p, _ = two_rounds
    xs = key(p, "R01")
    assert xs.status == EventStatus.EXPIRED
    assert "C10736042" in xs.snapshot_ids
    assert any(a["action"] == "attach_source" and "只存档" in a["detail"] for a in p.store.audit())


def test_late_source_strengthens_approved_event():
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(seed(), NOW, "manual")
    p.approve_topic(key(p, "R01").id, "运营", NOW)
    p.run_intake(R2["items"], NOW2, R2["round_id"])
    xs = key(p, "R01")
    assert xs.status == EventStatus.SELECTED and "C10736042" in xs.snapshot_ids


def test_new_candidate_from_rss(two_rounds):
    p, _ = two_rounds
    ev = key(p, "C10736055")
    assert ev.status == EventStatus.TO_SELECT and ev.verification["has_official"]
    scripts = p.approve_topic(ev.id, "运营", NOW2)
    assert scripts[-1].status == ScriptStatus.AWAITING_FINAL


def test_pending_event_reverified_when_second_source_arrives():
    p = Pipeline(Store(), ReplayLLM())
    one = {**seed()[3], "published_at": "2026-11-20T10:00:00+08:00"}   # 雪假只来了一条 B 级来源
    p.run_intake([one], NOW, "manual")
    ev = key(p, "R04")
    assert ev.status == EventStatus.PENDING
    p.run_intake([{**seed()[4]}], NOW, "manual")       # 光明网（A 级）补充进来
    assert "R05" in ev.snapshot_ids and ev.status == EventStatus.TO_SELECT


def test_return_script_replay_example():
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(seed(), NOW, "manual")
    ev = key(p, "R04")
    sc = p.approve_topic(ev.id, "运营", NOW)[-1]
    versions = p.return_script(sc.id, "运营", "钩子不够抓人", NOW)
    assert versions[-1].version == 2 and versions[-1].status == ScriptStatus.AWAITING_FINAL
    assert "真的能放假" in versions[-1].content["hook"]


def test_state_survives_restart(tmp_path):
    db = str(tmp_path / "eggy.db")
    p = Pipeline(Store(db), ReplayLLM())
    p.run_intake(seed(), NOW, "manual")
    p2 = Pipeline(Store(db), ReplayLLM())
    assert set(p2.events) == set(p.events) and set(p2.scripts) == set(p.scripts)
    xs = key(p2, "R01")
    p2.approve_topic(xs.id, "运营", NOW)
    assert p2.final_approve(f"{xs.id}-douyin-v2", "运营", NOW).status == ScriptStatus.APPROVED


# ---------- 定时采集 ----------
RSS = b"""<rss><channel>
<item><title>\xe5\x93\x88\xe5\xb0\x94\xe6\xbb\xa8\xe6\xbb\x91\xe9\x9b\xaa</title><link>https://www.chinanews.com.cn/ty/2026/11-22/10736055.shtml</link>
<pubDate>Sun, 22 Nov 2026 14:58:08 +0800</pubDate></item>
<item><title>other</title><link>https://www.chinanews.com.cn/cj/2026/11-22/1.shtml</link></item>
</channel></rss>"""


def test_parse_rss():
    items = parse_rss(RSS, "中国新闻网")
    assert items[0]["id"] == "C10736055" and items[0]["published_at"].startswith("2026-11-22T14:58")


def test_scheduler_dedups_by_url_and_reports_errors():
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(seed(), NOW, "manual")
    p.run_intake(R2["items"], NOW2, R2["round_id"])
    res = run_once(p, NOW2, fetch=lambda url: RSS)
    assert res["new"] == 1          # 哈尔滨那条已经入库，按 URL 去重
    res = run_once(p, NOW2, fetch=lambda url: (_ for _ in ()).throw(TimeoutError("超时")))
    assert res["errors"] and res["new"] == 0


# ---------- 存档、推送、回流 ----------
def test_archive_and_verify(tmp_path):
    store = Store()
    p = Pipeline(store, ReplayLLM())
    p.run_intake(seed()[:1], NOW, "manual")
    snap = store.snapshots()[0]
    rec = archive(snap, lambda url: b"<html>v1</html>", store, NOW, folder=tmp_path)
    assert rec["status"] == "ok" and len(rec["sha256"]) == 64 and Path(rec["file"]).exists()
    assert verify_archive(rec, lambda url: b"<html>v1</html>") == "same"
    assert verify_archive(rec, lambda url: b"<html>v2</html>") == "changed"
    bad = archive(snap, lambda url: (_ for _ in ()).throw(OSError("404")), store, NOW, folder=tmp_path)
    assert bad["status"] == "failed"
    assert wayback_lookup("https://a.b/c") == "https://web.archive.org/web/*/https://a.b/c"


def test_notifier_never_pretends_to_send():
    store = Store()
    assert Notifier(store, webhook="").send("hi", "E01", NOW) is False
    assert store.audit()[-1]["action"] == "notify_skipped"
    sent = []
    assert Notifier(store, webhook="https://hook", post=lambda u, b: sent.append(b)).send("hi", "E01", NOW)
    assert sent[0]["msg_type"] == "text"


def test_pipeline_pushes_new_candidates():
    sent = []
    store = Store()
    p = Pipeline(store, ReplayLLM(), notifier=Notifier(store, webhook="https://hook", post=lambda u, b: sent.append(b)))
    p.run_intake(seed(), NOW, "manual")
    texts = [b["content"]["text"] for b in sent]
    assert len(texts) == 3 and any("快速通道" in t for t in texts)


def test_feedback_suggestions():
    audit = [{"action": "decline_topic", "detail": "与游戏关联弱"}] * 2 + [{"action": "return_script", "detail": "x"}]
    s = feedback.suggestions(audit)
    assert s[0]["ready"] and "关联" in s[0]["suggest"] and not s[1]["ready"]


def test_eval_set_all_pass():
    res = evalset.run()
    failed = [r for r in res["rows"] if not r["ok"]]
    assert not failed, failed
    assert res["catch_rate"] == 1 and res["false_block_rate"] == 0
