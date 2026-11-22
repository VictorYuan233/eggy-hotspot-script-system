"""③ 分类核验。

新闻事件类：信源分级 + 多源交叉 + 辟谣库比对；模型抽取事实，规则检查每条事实
  确实出自它声称的来源（来源 ID 存在、数字能在来源里找到）。
梗与话题类：没有权威来源可交叉，改为核验出处可查、热度真实（跨平台出现）、含义无害。
只有“已核验”的事实才会进入脚本。
"""
from __future__ import annotations

from datetime import datetime

from . import config, rules
from .llm import LLM
from .models import AuditEntry, Event, EventStatus, Fact, Snapshot
from .store import Store


def _sources_block(snaps: list[Snapshot]) -> str:
    return "\n".join(f"[{s.id}] {s.publisher}｜{s.published_at[:10]}｜{s.title}：{s.summary}" for s in snaps)


def check_fact(fact: dict, snaps: dict[str, Snapshot]) -> str | None:
    """规则检查一条模型抽出的事实。返回问题描述，没问题返回 None。"""
    ids = fact.get("source_ids") or []
    if not ids:
        return "没有标注来源"
    missing = [i for i in ids if i not in snaps]
    if missing:
        return f"标注的来源不存在：{missing}"
    cited_text = "".join(snaps[i].title + snaps[i].summary for i in ids)
    unknown = {n for n in rules.numbers(fact["text"]) if n not in rules.numbers(cited_text)}
    if unknown:
        return f"数字在来源里找不到：{sorted(unknown)}"
    return None


def verify_news(ev: Event, snaps: list[Snapshot], llm: LLM) -> list[tuple[str, str, str]]:
    log = []
    cfg = config.rules()["verification"]
    linked = [s for s in snaps if s.url]
    tiers = {s.id: rules.source_tier(s.url) for s in snaps}
    domains = {rules.domain(s.url) for s in linked}
    has_official = any(t in ("A", "G") for t in tiers.values())
    enough = len(domains) >= cfg["news_min_independent_sources"] or (
        cfg["official_source_counts_alone"] and has_official)
    ev.verification = {"kind": "news", "tiers": tiers, "independent_domains": sorted(domains),
                       "has_official": has_official}

    if not enough:
        ev.status = EventStatus.PENDING
        ev.status_reason = f"来源不足：独立来源 {len(domains)} 个，且没有官方来源"
        log.append(("system", "verify_pending", ev.status_reason))
        return log

    out = llm.complete_json("facts", ev.key, config.render("extract_facts", sources=_sources_block(snaps)))
    by_id = {s.id: s for s in snaps}
    kept, dropped = [], []
    for raw in out.get("facts", []):
        problem = check_fact(raw, by_id)
        if problem:
            dropped.append(f"{raw.get('text', '')[:30]}…（{problem}）")
        else:
            kept.append(Fact(id=f"F{len(kept) + 1}", text=raw["text"], source_ids=raw["source_ids"]))
    ev.facts = kept
    ev.verification["dropped_facts"] = dropped
    log.append(("llm", "extract_facts", f"抽出 {len(kept) + len(dropped)} 条，规则保留 {len(kept)} 条"))
    for d in dropped:
        log.append(("system", "drop_fact", d))
    if kept:
        ev.status, ev.status_reason = EventStatus.VERIFIED, f"{len(domains)} 个独立来源，{len(kept)} 条事实已绑定证据"
    else:
        ev.status, ev.status_reason = EventStatus.DOUBTFUL, "没有能绑定到来源的事实"
    return log


def verify_meme(ev: Event, snaps: list[Snapshot], llm: LLM) -> list[tuple[str, str, str]]:
    log = []
    cfg = config.rules()["verification"]
    origin = min((s for s in snaps if s.url), key=lambda s: s.published_at, default=None)
    platforms = {s.platform for s in snaps if s.url}
    items = "\n".join(f"- [{s.id}] {s.publisher}｜{s.title}：{s.summary}" for s in snaps)
    out = llm.complete_json("meme_check", ev.key,
                            config.render("meme_check", game=config.game_setting()["game"], items=items))
    ev.verification = {
        "kind": "meme",
        "origin": {"snapshot_id": origin.id, "url": origin.url, "publisher": origin.publisher} if origin else None,
        "platforms": sorted(platforms),
        "harmless": out.get("harmless", False),
        "involves_real_person": out.get("involves_real_person", False),
        "real_persons": out.get("real_persons", []),
        "third_party_work": out.get("third_party_work", ""),
        "explanation": out.get("explanation", ""),
    }
    log.append(("llm", "meme_check", out.get("explanation", "")))
    # 规则复核：模型漏报的真人姓名，用名单补上
    rule_persons = rules.real_person_hits("".join(s.title + s.summary for s in snaps))
    if rule_persons:
        ev.verification["involves_real_person"] = True
        ev.verification["real_persons"] = sorted(set(ev.verification["real_persons"]) | set(rule_persons))
    if ev.verification["involves_real_person"]:
        ev.flags.append("legal_required")
        log.append(("system", "flag_legal", "涉及真人：" + "、".join(ev.verification["real_persons"])))

    if origin is None:
        ev.status, ev.status_reason = EventStatus.PENDING, "找不到可存档的出处"
    elif not ev.verification["harmless"]:
        ev.status, ev.status_reason = EventStatus.DOUBTFUL, "含义可能有害：" + ev.verification["explanation"]
    elif len(platforms) < cfg["meme_min_platforms"]:
        ev.status, ev.status_reason = EventStatus.PENDING, f"只在 {len(platforms)} 个平台出现，热度真实性待确认"
    else:
        ev.status = EventStatus.VERIFIED
        ev.status_reason = f"出处已存档（{origin.publisher}），{len(platforms)} 个平台出现，含义无害"
    return log


def verify(ev: Event, snaps: list[Snapshot], llm: LLM, store: Store, now: datetime) -> None:
    text = "".join(s.title + s.summary for s in snaps)
    debunk = rules.debunk_match(text)
    if debunk:
        ev.status = EventStatus.REJECTED
        ev.status_reason = f"命中辟谣库：{debunk['debunk']}（{debunk['source']}）"
        ev.verification = {"kind": ev.type, "debunk": debunk}
        log = [("system", "debunk_reject", ev.status_reason)]
    elif ev.type == "meme":
        log = verify_meme(ev, snaps, llm)
    else:
        log = verify_news(ev, snaps, llm)
    for actor, action, detail in log:
        store.log(AuditEntry(now.isoformat(), actor, action, ev.id, detail))
