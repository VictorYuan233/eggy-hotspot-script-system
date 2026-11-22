"""流程编排：把各模块串起来，并实现两个人工确认节点和快速通道。"""
from __future__ import annotations

from datetime import datetime

from . import config
from .clustering import build_events
from .collector import Collector
from .generator import generate
from .llm import LLM
from .models import (EVENT_TRANSITIONS, SCRIPT_TRANSITIONS, AuditEntry, Event, EventStatus, Script,
                     ScriptStatus, check_transition, to_dict)
from .reviewer import review_loop
from .scorer import pick_fast_lane, score_event
from .store import Store
from .verifier import verify

DECLINE_REASONS = ["与游戏关联弱", "调性不合", "风险太高", "时效已过", "已有同类内容", "其他"]


class HumanGateError(PermissionError):
    pass


class Pipeline:
    def __init__(self, store: Store, llm: LLM):
        self.store = store
        self.llm = llm
        self.events: dict[str, Event] = {}
        self.scripts: dict[str, Script] = {}
        self._ctx: dict[str, tuple] = {}   # event_id -> (fact_card, settings)

    # ---------- 工具 ----------
    def _log(self, now: datetime, actor: str, action: str, target: str, detail: str = "") -> None:
        self.store.log(AuditEntry(now.isoformat(), actor, action, target, detail))

    def _snaps(self, ev: Event):
        by_id = {s.id: s for s in self.store.snapshots()}
        return [by_id[i] for i in ev.snapshot_ids]

    def _set_event(self, ev: Event, status: EventStatus, reason: str = "") -> None:
        check_transition(EVENT_TRANSITIONS, ev.status, status)
        ev.status, ev.status_reason = status, reason or ev.status_reason

    def _save(self) -> None:
        for ev in self.events.values():
            self.store.put("events", ev.id, ev)
        for sc in self.scripts.values():
            self.store.put("scripts", sc.id, sc, event_id=sc.event_id)

    # ---------- 自动流水线 ----------
    def run_intake(self, raw_items: list[dict], now: datetime) -> list[Event]:
        Collector(self.store).ingest(raw_items, now)
        events = build_events(self.store, self.llm, now)
        for ev in events:
            self.events[ev.id] = ev
            if ev.status != EventStatus.NEW:
                continue          # 红线预过滤或已过期
            self._set_event(ev, EventStatus.VERIFYING)
            verify(ev, self._snaps(ev), self.llm, self.store, now)
            if ev.status == EventStatus.VERIFIED:
                score_event(ev, self._snaps(ev), self.llm, self.store, now)
        for ev in pick_fast_lane(events):
            self._log(now, "system", "fast_lane", ev.id,
                      f"高时效（{ev.expire_at[11:16]} 过期），选题前预生成草稿")
            for platform in ev.score["platforms"]:
                self._produce(ev, platform, now, pregenerated=True)
        self._save()
        return events

    def _produce(self, ev: Event, platform: str, now: datetime, pregenerated: bool) -> list[Script]:
        script, card, settings = generate(ev, self._snaps(ev), platform, self.llm, pregenerated)
        self._ctx[ev.id] = (card, settings)
        self._log(now, "llm", "generate", script.id, "预生成" if pregenerated else "生成")
        versions = review_loop(script, ev.key, card, settings, self.llm)
        for v in versions:
            self.scripts[v.id] = v
            r = v.reviews[-1] if v.reviews else {}
            n_rule = len([i for i in r.get("rule_issues", []) if i["severity"] == "block"])
            self._log(now, "system", "review", v.id,
                      f"规则问题 {n_rule} 个；模型评审{'通过' if r.get('judge', {}).get('passed') else '未通过'}；"
                      f"结果：{v.status.value}")
        return versions

    # ---------- 人工节点一：选题确认 ----------
    def approve_topic(self, event_id: str, operator: str, now: datetime,
                      platforms: list[str] | None = None) -> list[Script]:
        ev = self.events[event_id]
        self._set_event(ev, EventStatus.SELECTED, f"{operator} 确认选题")
        self._log(now, operator, "approve_topic", ev.id)
        result = [s for s in self.scripts.values() if s.event_id == ev.id]
        if not result:
            for platform in platforms or ev.score.get("platforms") or ["douyin"]:
                result += self._produce(ev, platform, now, pregenerated=False)
        self._save()
        return result

    def decline_topic(self, event_id: str, operator: str, reason: str, now: datetime) -> None:
        if reason not in DECLINE_REASONS:
            raise ValueError(f"拒绝理由必须是固定选项之一：{DECLINE_REASONS}")
        ev = self.events[event_id]
        self._set_event(ev, EventStatus.DECLINED, f"{operator} 拒绝：{reason}")
        self._log(now, operator, "decline_topic", ev.id, reason)   # 回流给打分
        self._save()

    # ---------- 人工节点二：终审 ----------
    def final_approve(self, script_id: str, operator: str, now: datetime,
                      legal_signoff_by: str | None = None) -> Script:
        sc = self.scripts[script_id]
        ev = self.events[sc.event_id]
        if ev.status != EventStatus.SELECTED:
            raise HumanGateError("选题还没有被运营确认，不能终审")
        needs_legal = "legal_required" in sc.flags or "legal_required" in ev.flags
        if needs_legal and not legal_signoff_by:
            raise HumanGateError("该脚本涉及真人或联动 IP，需要法务会签")
        check_transition(SCRIPT_TRANSITIONS, sc.status, ScriptStatus.APPROVED)
        sc.status = ScriptStatus.APPROVED
        first = min(s.fetched_at for s in self._snaps(ev))
        minutes = (now - datetime.fromisoformat(first)).total_seconds() / 60
        sla = config.rules()["urgency"][
            "end_to_end_sla_minutes_high" if ev.urgency == "high" else "end_to_end_sla_minutes_normal"]
        detail = f"从入库到终稿 {minutes:.0f} 分钟（时限 {sla}）" + (f"；法务会签：{legal_signoff_by}" if needs_legal else "")
        self._log(now, operator, "final_approve", sc.id, detail)
        if minutes > sla:
            self._log(now, "system", "sla_breach", sc.id, detail)
        self._save()
        return sc

    def return_script(self, script_id: str, operator: str, comment: str, now: datetime) -> list[Script]:
        sc = self.scripts[script_id]
        check_transition(SCRIPT_TRANSITIONS, sc.status, ScriptStatus.RETURNED)
        sc.status = ScriptStatus.RETURNED
        self._log(now, operator, "return_script", sc.id, comment)
        card, settings = self._ctx[sc.event_id]
        ev = self.events[sc.event_id]
        versions = review_loop(sc, ev.key, card, settings, self.llm,
                               extra_issues=[{"severity": "block", "rule": "运营退回", "problem": comment}])
        for v in versions:
            self.scripts[v.id] = v
        self._save()
        return versions

    # ---------- 数据回流 ----------
    def record_published(self, script_id: str, operator: str, metrics: dict, now: datetime) -> None:
        sc = self.scripts[script_id]
        check_transition(SCRIPT_TRANSITIONS, sc.status, ScriptStatus.PUBLISHED)
        sc.status = ScriptStatus.PUBLISHED
        self._log(now, operator, "published", sc.id, str(metrics))
        self._save()

    def export_state(self) -> dict:
        return {
            "snapshots": [to_dict(s) for s in self.store.snapshots()],
            "events": [to_dict(e) for e in self.events.values()],
            "scripts": [to_dict(s) for s in self.scripts.values()],
            "audit": self.store.audit(),
            "contexts": {eid: {"fact_card": card, "setting_ids": [e["id"] for e in settings]}
                         for eid, (card, settings) in self._ctx.items()},
        }
