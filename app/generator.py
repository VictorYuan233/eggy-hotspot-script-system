"""⑥ 脚本生成：事实卡（只含已核验内容）+ 设定检索 + 平台模板 → 结构化脚本。"""
from __future__ import annotations

from . import config
from .llm import LLM
from .models import Event, Script, ScriptStatus, Snapshot
from .scorer import select_settings, settings_block


def fact_card(ev: Event, snaps: list[Snapshot]) -> list[dict]:
    """新闻类用已核验事实；梗类用出处和用法（同样带来源 ID）。"""
    if ev.facts:
        return [{"id": f.id, "text": f.text, "source_ids": f.source_ids} for f in ev.facts]
    return [{"id": f"F{i}", "text": f"{s.publisher}：{s.summary}", "source_ids": [s.id]}
            for i, s in enumerate(sorted(snaps, key=lambda s: s.id), start=1)]


def fact_card_block(card: list[dict]) -> str:
    return "\n".join(f"[{f['id']}] {f['text']}（来源：{','.join(f['source_ids'])}）" for f in card)


def generate(ev: Event, snaps: list[Snapshot], platform: str, llm: LLM, pregenerated: bool) -> tuple[Script, list[dict], list[dict]]:
    r = config.rules()
    p = r["platforms"][platform]
    card = fact_card(ev, snaps)
    text = "".join(s.title + s.summary for s in snaps) + ev.score.get("angle", "")
    settings = select_settings(text)
    prompt = config.render(
        "generate_script", game=config.game_setting()["game"], platform_name=p["name"],
        duration_min=p["duration_sec"][0], duration_max=p["duration_sec"][1], aspect=p["aspect"],
        max_title=p["max_title_chars"], max_tags=p["max_tags"], hook_sec=p["hook_within_sec"],
        angle=ev.score.get("angle", ""), fact_card=fact_card_block(card),
        settings=settings_block(settings))
    ev.context = {"fact_card": card, "setting_ids": [e["id"] for e in settings]}
    content = llm.complete_json("generate", f"{ev.key}:{platform}", prompt, role="generator")
    script = Script(
        id=f"{ev.id}-{platform}-v1", event_id=ev.id, platform=platform, version=1, content=content,
        status=ScriptStatus.PREGENERATED if pregenerated else ScriptStatus.DRAFT,
        pregenerated=pregenerated, setting_version=config.game_setting()["version"],
        prompt_version=r["version"])
    return script, card, settings
