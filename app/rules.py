"""确定性规则：红线、广告法、防沉迷、第三方 IP、真人、竞品、信源分级、辟谣库。

这些判断都不用大模型：结果稳定、可解释、出了问题能查清是哪条规则命中。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import config


def _hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w and w in text]


def redline_hits(text: str) -> list[str]:
    """返回命中的红线，形如 ['灾难事故:身亡']。"""
    r = config.rules()
    hits = [f"{cat}:{w}" for cat, words in r["redline_topics"].items() for w in _hits(text, words)]
    hits += [f"竞品:{w}" for w in _hits(text, r["competitors"])]
    hits += [f"未授权第三方IP:{w}" for w in _hits(text, r["third_party_ip"]["never_authorized"])]
    return hits


def past_collab_ip_hits(text: str) -> list[str]:
    return _hits(text, config.rules()["third_party_ip"]["past_collab_need_legal"])


def real_person_hits(text: str) -> list[str]:
    return _hits(text, config.rules()["real_person_names"])


def ad_law_hits(text: str) -> list[str]:
    for ok in config.rules().get("ad_law_exceptions", []):   # “第一次”“第一时间”等不是比较级
        text = text.replace(ok, "")
    return _hits(text, config.rules()["ad_law_words"])


def anti_addiction_hits(text: str) -> list[str]:
    return _hits(text, config.rules()["anti_addiction_words"])


def pay_inducement_hits(text: str) -> list[str]:
    return _hits(text, config.rules()["pay_inducement_words"])


def own_ops_event(text: str) -> bool:
    """自家运营事件（停服、渠道停运等）：走官方公告，不做热点营销。"""
    r = config.rules()
    game = config.game_setting()["game"]
    return game in text and bool(_hits(text, r["own_ops_event_keywords"]))


def debunk_match(text: str) -> dict | None:
    for item in config.rules()["debunk_db"]:
        if all(k in text for k in item["claim_keywords"]):
            return item
    return None


def source_tier(url: str) -> str:
    """A：官方媒体；B：主流媒体；G：游戏官方渠道；C：其他；N：无链接。"""
    if not url:
        return "N"
    tiers = config.rules()["source_tiers"]
    for prefix in tiers["official_game"]:
        if prefix in url:
            return "G"
    host = urlparse(url).netloc.lower()
    for tier in ("A", "B"):
        if any(host == d or host.endswith("." + d) for d in tiers[tier]):
            return tier
    return "C"


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    return set(NUM_RE.findall(text))
