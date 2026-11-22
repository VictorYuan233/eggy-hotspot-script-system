"""覆盖 C1 方案里的关键约束：来源可追溯、红线、分类核验、快速通道、两道人工确认、规则检查。"""
import json
import sqlite3
from datetime import datetime

import pytest

from app import rules
from app.collector import Collector, SourceNotAuthorized, SourceRegistry
from app.config import DATA_DIR
from app.llm import ReplayLLM, parse_json
from app.models import EventStatus, ScriptStatus, Snapshot, TransitionError
from app.pipeline import HumanGateError, Pipeline
from app.reviewer import rule_check
from app.store import Store
from app.verifier import check_fact

NOW = datetime.fromisoformat("2026-11-22T21:45:00+08:00")


def seed_items():
    seed = json.loads((DATA_DIR / "seed_hotspots.json").read_text(encoding="utf-8"))
    return [{**s, "fetched_at": seed["collected_at"]} for s in seed["snapshots"]]


@pytest.fixture
def pipe():
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(seed_items(), NOW)
    return p


def by_key(p, key):
    return next(e for e in p.events.values() if e.key == key)


# ---------- 采集与证据 ----------
def test_snapshots_are_immutable():
    store = Store()
    store.add_snapshot(Snapshot(id="X1", source_id="manual_submit", title="t", url="u", publisher="p",
                                platform="web", published_at=NOW.isoformat(), fetched_at=NOW.isoformat(),
                                summary="s"))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE snapshots SET payload='x' WHERE id='X1'")
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM snapshots WHERE id='X1'")


def test_unauthorized_source_is_skipped_and_logged():
    store = Store()
    item = {**seed_items()[0], "id": "W1", "source_id": "weibo_open_api"}
    assert Collector(store).ingest([item], NOW) == []
    assert store.audit()[-1]["action"] == "skip_unauthorized"
    with pytest.raises(SourceNotAuthorized):
        SourceRegistry().check("weibo_open_api", NOW)


def test_expired_authorization_is_rejected():
    reg = SourceRegistry({"sources": [{"id": "x", "status": "active", "auth": "", "valid_until": "2026-01-01"}]})
    with pytest.raises(SourceNotAuthorized):
        reg.check("x", NOW)


def test_rss_pull_goes_through_registry():
    rss = "<rss><channel><item><title>标题</title><link>https://example.com/a</link>" \
          "<description>摘要</description></item></channel></rss>"
    store = Store()
    saved = Collector(store).pull_rss("https://example.com/feed", lambda url: rss, NOW)
    assert saved[0].source_id == "public_rss" and saved[0].url == "https://example.com/a"


# ---------- 归并、红线、过期 ----------
def test_events_are_merged_across_sources(pipe):
    assert by_key(pipe, "R01").snapshot_ids == ["R01", "R02", "R03"]
    assert set(by_key(pipe, "R04").snapshot_ids) == {"R04", "R05", "R06"}   # 模型建议合并 R06
    assert set(by_key(pipe, "R07").snapshot_ids) == {"R07", "R08"}


@pytest.mark.parametrize("key,reason", [("R16", "暴力犯罪"), ("R17", "灾难事故"), ("R11", "未授权第三方IP")])
def test_redline_events_rejected_before_verification(pipe, key, reason):
    ev = by_key(pipe, key)
    assert ev.status == EventStatus.REJECTED and reason in ev.status_reason
    assert not ev.facts


def test_expired_events_go_offline(pipe):
    assert by_key(pipe, "R12").status == EventStatus.EXPIRED


def test_own_ops_event_not_marketed(pipe):
    ev = by_key(pipe, "R09")
    assert ev.status == EventStatus.NOT_RECOMMENDED and "own_ops" in ev.flags


# ---------- 分类核验 ----------
def test_news_needs_multiple_sources_and_facts_bind_evidence(pipe):
    ev = by_key(pipe, "R04")
    assert ev.status in (EventStatus.TO_SELECT, EventStatus.SELECTED)
    assert ev.verification["has_official"]
    for f in ev.facts:
        assert f.source_ids and all(s in ev.snapshot_ids for s in f.source_ids)


def test_fact_with_numbers_missing_from_source_is_dropped(pipe):
    ev = by_key(pipe, "R01")
    assert len(ev.facts) == 3
    assert "12月7日" in ev.verification["dropped_facts"][0]


def test_check_fact_rules():
    snap = Snapshot(id="A", source_id="m", title="雪假连休5天", url="u", publisher="p", platform="w",
                    published_at="", fetched_at="", summary="")
    assert check_fact({"text": "连休5天", "source_ids": ["A"]}, {"A": snap}) is None
    assert "数字" in check_fact({"text": "连休9天", "source_ids": ["A"]}, {"A": snap})
    assert "不存在" in check_fact({"text": "连休5天", "source_ids": ["B"]}, {"A": snap})
    assert "没有标注" in check_fact({"text": "连休5天", "source_ids": []}, {"A": snap})


def test_meme_verified_by_origin_platforms_and_flags_real_person(pipe):
    ev = by_key(pipe, "R07")
    assert ev.type == "meme" and ev.verification["harmless"]
    assert len(ev.verification["platforms"]) >= 2
    assert "legal_required" in ev.flags


def test_single_source_news_is_pending():
    p = Pipeline(Store(), ReplayLLM())
    item = {"id": "R13", "source_id": "manual_submit", "title": "葡萄牙主帅回应C罗从国家队退役",
            "url": "https://sina.cn/media/2028810631", "publisher": "新浪", "platform": "weibo",
            "published_at": "2026-11-22T10:00:00+08:00", "summary": "热苏斯表示退役是个人决定。"}
    p.run_intake([item], NOW)
    ev = by_key(p, "R13")
    assert ev.status == EventStatus.PENDING


def test_debunked_rumor_rejected(pipe):
    ev = by_key(pipe, "R18")
    assert ev.status == EventStatus.REJECTED and "辟谣" in ev.status_reason


# ---------- 选题与快速通道 ----------
def test_fast_lane_pregenerates_only_high_urgency(pipe):
    fast = [e for e in pipe.events.values() if e.fast_lane]
    assert [e.key for e in fast] == ["R01"]
    pre = [s for s in pipe.scripts.values() if s.pregenerated]
    assert pre and all(s.event_id == fast[0].id for s in pre)
    assert not [s for s in pipe.scripts.values() if s.event_id == by_key(pipe, "R04").id]


# ---------- 脚本评审 ----------
def test_review_loop_fixes_issues(pipe):
    ev = by_key(pipe, "R01")
    v1 = pipe.scripts[f"{ev.id}-douyin-v1"]
    rules_hit = {i["rule"] for i in v1.reviews[0]["rule_issues"]}
    assert {"广告法极限词", "数字溯源"} <= rules_hit
    assert not v1.reviews[0]["judge"]["passed"]
    v2 = pipe.scripts[f"{ev.id}-douyin-v2"]
    assert v2.status == ScriptStatus.AWAITING_FINAL and not v2.reviews[0]["rule_issues"]


def _content(**over):
    base = {"title": "小雪到", "hook": "钩子", "cta": "", "tags": ["#a"], "fact_ids": ["F1"],
            "setting_ids": ["S04"], "shots": [{"t": "0-3s", "visual": "画面", "line": "小雪到啦"},
                                               {"t": "3-20s", "visual": "画面", "line": "一起玩雪"}]}
    base.update(over)
    return base


CARD = [{"id": "F1", "text": "小雪在11月22日", "source_ids": ["R01"]}]
SETTINGS = [{"id": "S04", "category": "称呼", "title": "t", "content": "蛋仔们"}]


@pytest.mark.parametrize("over,rule", [
    ({"title": "史上最强小雪派对"}, "广告法极限词"),
    ({"cta": "今晚通宵玩"}, "防沉迷"),
    ({"fact_ids": ["F9"]}, "事实溯源"),
    ({"setting_ids": ["S99"]}, "设定溯源"),
    ({"hook": "气温降了8度"}, "数字溯源"),
    ({"shots": [{"t": "0-3s", "visual": "", "line": "a"}, {"t": "3-90s", "visual": "", "line": "b"}]}, "时长"),
    ({"shots": [{"t": "0-6s", "visual": "", "line": "a"}, {"t": "6-20s", "visual": "", "line": "b"}]}, "钩子"),
    ({"tags": ["#1", "#2", "#3", "#4", "#5", "#6"]}, "标签"),
    ({"cta": "比元梦之星好玩"}, "红线"),
])
def test_rule_check_catches(over, rule):
    issues = rule_check(_content(**over), "douyin", CARD, SETTINGS)
    assert rule in {i["rule"] for i in issues if i["severity"] == "block"}


def test_rule_check_clean_script_passes():
    assert rule_check(_content(), "douyin", CARD, SETTINGS) == []


def test_real_person_needs_legal_not_block():
    issues = rule_check(_content(cta="今天也辛苦了，厉娜"), "douyin", CARD, SETTINGS)
    assert [i["severity"] for i in issues] == ["legal"]


# ---------- 两道人工确认 ----------
def test_final_approve_requires_topic_approval(pipe):
    sc = pipe.scripts[f"{by_key(pipe, 'R01').id}-douyin-v2"]
    with pytest.raises(HumanGateError):
        pipe.final_approve(sc.id, "运营", NOW)


def test_full_flow_with_sla_and_legal(pipe):
    xs = by_key(pipe, "R01")
    pipe.approve_topic(xs.id, "运营", NOW)
    sc = pipe.final_approve(f"{xs.id}-douyin-v2", "运营", NOW)
    assert sc.status == ScriptStatus.APPROVED
    ln = by_key(pipe, "R07")
    s = pipe.approve_topic(ln.id, "运营", NOW)[-1]
    with pytest.raises(HumanGateError):
        pipe.final_approve(s.id, "运营", NOW)
    assert pipe.final_approve(s.id, "运营", NOW, legal_signoff_by="法务").status == ScriptStatus.APPROVED
    actions = [a["action"] for a in pipe.store.audit()]
    assert actions.count("final_approve") == 2


def test_cannot_approve_superseded_version(pipe):
    xs = by_key(pipe, "R01")
    pipe.approve_topic(xs.id, "运营", NOW)
    with pytest.raises(TransitionError):
        pipe.final_approve(f"{xs.id}-douyin-v1", "运营", NOW)


def test_decline_requires_fixed_reason(pipe):
    ev = by_key(pipe, "R04")
    with pytest.raises(ValueError):
        pipe.decline_topic(ev.id, "运营", "随便", NOW)
    pipe.decline_topic(ev.id, "运营", "调性不合", NOW)
    assert ev.status == EventStatus.DECLINED


# ---------- 其他 ----------
def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('结果如下：{"a": 2} 以上') == {"a": 2}


def test_source_tiers():
    assert rules.source_tier("https://www.news.cn/x") == "A"
    assert rules.source_tier("https://news.sina.cn/x") == "B"
    assert rules.source_tier("https://example.com") == "C"
    assert rules.source_tier("") == "N"
