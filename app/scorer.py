"""④ 选题评估：红线一票否决 → 模型打分 → 规则加权 → 排序；并选出快速通道。"""
from __future__ import annotations

from datetime import datetime

from . import config, rules
from .llm import LLM
from .models import AuditEntry, Event, EventStatus, Snapshot
from .store import Store


def momentum(series: list[float]) -> float:
    """热度上升速度，0-100。下降为 0，持平为 50。"""
    if len(series) < 2 or max(series) == 0:
        return 50.0
    slope = (series[-1] - series[-2]) / max(series)
    return max(0.0, min(100.0, 50 + slope * 250))


def timeliness(ev: Event, snaps: list[Snapshot], now: datetime) -> float:
    """剩余窗口占总窗口的比例，0-100。"""
    start = min(datetime.fromisoformat(s.published_at) for s in snaps)
    end = datetime.fromisoformat(ev.expire_at)
    total = (end - start).total_seconds()
    left = (end - now).total_seconds()
    return max(0.0, min(100.0, 100 * left / total)) if total > 0 else 0.0


def select_settings(text: str, limit: int = 6) -> list[dict]:
    """按关键词重合度检索设定。称呼口吻和禁区总是带上。"""
    entries = config.game_setting()["entries"]
    always = [e for e in entries if e["id"] == "S04" or e["category"] == "禁区"]
    ranked = sorted((e for e in entries if e not in always),
                    key=lambda e: -sum(k in text for k in e["keywords"]))
    picked = [e for e in ranked if sum(k in text for k in e["keywords"]) > 0][:limit]
    if not picked:
        picked = ranked[:2]  # 至少带上世界观
    return picked + always


def settings_block(entries: list[dict]) -> str:
    return "\n".join(f"[{e['id']}] {e['category']}·{e['title']}：{e['content']}" for e in entries)


def score_event(ev: Event, snaps: list[Snapshot], llm: LLM, store: Store, now: datetime) -> None:
    r = config.rules()
    text = "".join(s.title + s.summary for s in snaps) + "".join(f.text for f in ev.facts)
    log = []

    hits = rules.redline_hits(text)
    if hits:
        ev.status, ev.status_reason = EventStatus.REJECTED, "红线：" + "、".join(hits)
        log.append(("system", "redline_reject", ev.status_reason))
    elif rules.own_ops_event(text):
        ev.status = EventStatus.NOT_RECOMMENDED
        ev.status_reason = "自家运营事件（停运/停服等），应走官方公告，不做热点营销"
        ev.flags.append("own_ops")
        log.append(("system", "own_ops", ev.status_reason))
    else:
        facts = "\n".join(f"[{f.id}] {f.text}" for f in ev.facts) or \
            f"梗的出处：{ev.verification.get('origin')}；用法：{snaps[0].summary}"
        event_text = "\n".join(f"- {s.title}：{s.summary}" for s in snaps)
        out = llm.complete_json("score", ev.key, config.render(
            "score_topic", game=config.game_setting()["game"],
            settings=settings_block(select_settings(text)), event_type=ev.type,
            event=event_text, facts=facts))
        parts = {
            "relevance": float(out["relevance"]),
            "meme_potential": float(out["meme_potential"]),
            "tone_fit": float(out["tone_fit"]),
            "timeliness": round(timeliness(ev, snaps, now), 1),
            "heat_momentum": round(momentum(snaps[0].heat_series), 1),
        }
        w = r["scoring_weights"]
        total = round(sum(parts[k] * w[k] for k in w), 1)
        ev.score = {"parts": parts, "total": total, "angle": out.get("angle", ""),
                    "platforms": out.get("platforms", ["douyin"]), "reason": out.get("reason", ""),
                    "risks": out.get("risks", []), "heat_simulated": True}
        log.append(("llm", "score", f"相关度{parts['relevance']:.0f} 玩梗{parts['meme_potential']:.0f} "
                                    f"调性{parts['tone_fit']:.0f}"))
        if total >= r["recommend_threshold"]:
            ev.status, ev.status_reason = EventStatus.TO_SELECT, f"总分 {total}，进入候选看板"
        else:
            ev.status, ev.status_reason = EventStatus.NOT_RECOMMENDED, f"总分 {total}，低于推荐线 {r['recommend_threshold']}"
        log.append(("system", "rank", ev.status_reason))

    for actor, action, detail in log:
        store.log(AuditEntry(now.isoformat(), actor, action, ev.id, detail))


def pick_fast_lane(events: list[Event]) -> list[Event]:
    """高时效、进入候选看板、分数靠前的少数热点走快速通道。只取前 N 个以控制模型成本。"""
    n = config.rules()["urgency"]["fast_lane_top_n"]
    cands = [e for e in events if e.status == EventStatus.TO_SELECT and e.urgency == "high"]
    cands.sort(key=lambda e: -e.score["total"])
    for e in cands[:n]:
        e.fast_lane = True
    return cands[:n]
