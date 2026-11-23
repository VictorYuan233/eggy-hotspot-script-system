"""② 清洗与事件归并：去重、跨平台聚合成事件、红线预过滤、分类、时效估算。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import config, rules
from .llm import LLM
from .models import AuditEntry, Event, EventStatus, Snapshot
from .store import Store

STOP = set("的了是在和与及、，。！？：“”《》（）丨 ")


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text)


def bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i:i + 2] for i in range(len(t) - 1) if not set(t[i:i + 2]) & STOP}


def similarity(a: Snapshot, b: Snapshot) -> float:
    """标题+摘要的字二元组 Jaccard。正式环境可换成向量模型的语义相似度。"""
    x, y = bigrams(a.title + a.summary), bigrams(b.title + b.summary)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def cluster(snaps: list[Snapshot], threshold: float | None = None) -> list[list[Snapshot]]:
    threshold = threshold or config.rules()["clustering"]["similarity_threshold"]
    groups: list[list[Snapshot]] = []
    for s in sorted(snaps, key=lambda s: s.id):
        for g in groups:
            if max(similarity(s, m) for m in g) >= threshold:
                g.append(s)
                break
        else:
            groups.append([s])
    return groups


def llm_merge(groups: list[list[Snapshot]], existing: dict[str, Event], llm: LLM, key: str
              ) -> tuple[list[list[Snapshot]], dict[str, list[Snapshot]], str]:
    """让模型判断新簇之间、新簇与已有事件之间哪些是同一件事。

    返回 (合并后的新簇, {已有事件ID: 并入的快照}, 理由)。只接受键都存在的合并建议。
    """
    by_key = {g[0].id: g for g in groups}
    ev_by_key = {e.key: e for e in existing.values()}
    lines = [f"[{k}] " + "；".join(f"{s.title}（{s.summary[:40]}）" for s in g) for k, g in by_key.items()]
    lines += [f"[{e.key}] （已有事件）{e.title}" for e in existing.values()]
    out = llm.complete_json("merge", key, config.render("merge_events", clusters="\n".join(lines)))
    attach: dict[str, list[Snapshot]] = {}
    for keys in out.get("merges", []):
        keys = [k for k in keys if k in by_key or k in ev_by_key]
        if len(keys) < 2:
            continue
        head = keys[0]
        for k in keys[1:]:
            if k not in by_key:
                continue
            moved = by_key.pop(k)
            if head in by_key:
                by_key[head].extend(moved)
            elif head in ev_by_key:
                attach.setdefault(ev_by_key[head].id, []).extend(moved)
    merged = sorted(by_key.values(), key=lambda g: g[0].id)
    return merged, attach, out.get("reason", "")


def prefilter(snaps: list[Snapshot]) -> tuple[list[Snapshot], list[Snapshot]]:
    """RSS 全量新闻先做关键词粗筛；运营手动提交的条目不过滤。"""
    words = config.rules()["rss_prefilter"]["keywords"]
    keep, drop = [], []
    for s in snaps:
        if s.source_id != "public_rss" or any(w in s.title + s.summary for w in words):
            keep.append(s)
        else:
            drop.append(s)
    return keep, drop


def classify_rule(snaps: list[Snapshot]) -> str:
    text = "".join(s.title + s.summary for s in snaps)
    return "meme" if re.search(r"梗|热词|体”|句式|belike|口头禅|二创", text) else "news"


def estimate_window(event_type: str, snaps: list[Snapshot], now: datetime) -> tuple[datetime, str]:
    """返回 (过期时间, 紧急程度)。"""
    u = config.rules()["urgency"]
    latest = max(datetime.fromisoformat(s.published_at) for s in snaps)
    text = "".join(s.title + s.summary for s in snaps)
    if any(k in text for k in u["date_bound_keywords"]) and latest.date() >= (now - timedelta(days=1)).date():
        expire = now.replace(hour=23, minute=59, second=0, microsecond=0)  # 当天有效的热点
    else:
        hours = u["meme_window_hours"] if event_type == "meme" else u["news_window_hours"]
        expire = latest + timedelta(hours=hours)
    remaining = (expire - now).total_seconds() / 3600
    urgency = "high" if 0 < remaining <= u["high_window_hours"] else "normal"
    return expire, urgency


def build_events(store: Store, llm: LLM, now: datetime, new_snaps: list[Snapshot],
                 existing: dict[str, Event], round_id: str) -> tuple[list[Event], dict[str, list[Snapshot]]]:
    """增量建事件：新快照先粗筛，再与已有事件比对，剩下的聚成新事件。

    返回 (新事件, {已有事件ID: 新补充的快照})。
    """
    keep, drop = prefilter(new_snaps)
    if drop:
        store.log(AuditEntry(now.isoformat(), "system", "prefilter_skip", round_id,
                             f"粗筛未通过 {len(drop)} 条，只存档不进入流程"))
    threshold = config.rules()["clustering"]["similarity_threshold"]
    by_id = {s.id: s for s in store.snapshots()}
    attach: dict[str, list[Snapshot]] = {}
    rest = []
    for s in keep:   # 规则：和已有事件字面相似就直接并入
        hit = next((e for e in existing.values()
                    if max(similarity(s, by_id[i]) for i in e.snapshot_ids) >= threshold), None)
        if hit:
            attach.setdefault(hit.id, []).append(s)
        else:
            rest.append(s)
    groups = cluster(rest)
    if groups:
        try:
            groups, more, reason = llm_merge(groups, existing, llm, f"merge:{round_id}")
            for k, v in more.items():
                attach.setdefault(k, []).extend(v)
            store.log(AuditEntry(now.isoformat(), "llm", "merge_events", round_id, reason))
        except Exception as exc:  # 模型不可用时只用字面聚类
            store.log(AuditEntry(now.isoformat(), "system", "merge_fallback", round_id, str(exc)))

    events: list[Event] = []
    start = len(existing) + 1
    for i, group in enumerate(groups, start=start):
        key = group[0].id
        ev = Event(id=f"E{i:02d}", key=key, title=group[0].title,
                   snapshot_ids=[s.id for s in group])
        text = "".join(s.title + s.summary for s in group)
        log = []

        # 红线预过滤：命中就不再做核验，省掉无意义的模型调用
        hits = rules.redline_hits(text)
        if hits:
            ev.status, ev.status_reason = EventStatus.REJECTED, "红线预过滤：" + "、".join(hits)
            log.append(("system", "redline_reject", ev.status_reason))
        elif rules.own_ops_event(text):
            ev.status = EventStatus.NOT_RECOMMENDED
            ev.status_reason = "自家运营事件（停运/停服等），应走官方公告，不做热点营销"
            ev.flags.append("own_ops")
            log.append(("system", "own_ops", ev.status_reason))
        else:
            items = "\n".join(f"- [{s.id}] {s.title}：{s.summary}" for s in group)
            try:
                out = llm.complete_json("classify", key, config.render("classify_event", items=items))
                ev.type, ev.type_reason = out["type"], out.get("reason", "")
                log.append(("llm", "classify", f"{ev.type}：{ev.type_reason}"))
            except Exception as exc:  # 模型不可用时退回规则分类
                ev.type, ev.type_reason = classify_rule(group), f"规则分类（模型不可用：{exc}）"
                log.append(("system", "classify_fallback", ev.type))
            expire, ev.urgency = estimate_window(ev.type, group, now)
            ev.expire_at = expire.isoformat()
            if expire <= now:
                ev.status, ev.status_reason = EventStatus.EXPIRED, f"已于 {expire:%m-%d %H:%M} 过期"
                log.append(("system", "expired", ev.status_reason))

        for actor, action, detail in log:
            store.log(AuditEntry(now.isoformat(), actor, action, ev.id, detail))
        events.append(ev)
    return events, attach
