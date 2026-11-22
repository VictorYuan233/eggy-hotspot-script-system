"""⑦ 评估与修改：规则检查 + 独立模型评审 + 自动改稿（最多 N 轮），超出轮次标红转人工。"""
from __future__ import annotations

import json
import re
from dataclasses import replace

from . import config, rules
from .llm import LLM
from .models import Script, ScriptStatus, check_transition, SCRIPT_TRANSITIONS
from .scorer import settings_block
from .generator import fact_card_block

T_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*s")


def script_text(c: dict) -> str:
    """脚本里所有会被观众看到或听到的文字。"""
    parts = [c.get("title", ""), c.get("hook", ""), c.get("cta", ""), " ".join(c.get("tags", []))]
    for shot in c.get("shots", []):
        parts += [shot.get("visual", ""), shot.get("line", "")]
    return "\n".join(parts)


def rule_check(c: dict, platform: str, card: list[dict], settings: list[dict]) -> list[dict]:
    p = config.rules()["platforms"][platform]
    issues: list[dict] = []

    def add(severity: str, rule: str, problem: str, where: str = "") -> None:
        issues.append({"severity": severity, "rule": rule, "where": where, "problem": problem})

    text = script_text(c)
    # 时长与钩子
    spans = [T_RE.search(s.get("t", "")) for s in c.get("shots", [])]
    if not c.get("shots") or not all(spans):
        add("block", "时长", "分镜缺少形如 0-3s 的时间标注")
    else:
        end = max(float(m.group(2)) for m in spans)
        lo, hi = p["duration_sec"]
        if not lo <= end <= hi:
            add("block", "时长", f"总时长 {end:.0f} 秒，不在 {p['name']} 规范 {lo}-{hi} 秒内")
        speech = sum(len(re.sub(r"\W", "", s.get("line", ""))) for s in c["shots"])
        need = speech / p["speech_chars_per_sec"]
        if need > end * 1.1:
            add("block", "时长", f"台词约需 {need:.0f} 秒才能读完，超过视频时长 {end:.0f} 秒")
        first = spans[0]
        if float(first.group(1)) != 0 or float(first.group(2)) > p["hook_within_sec"] or not c.get("hook"):
            add("block", "钩子", f"前 {p['hook_within_sec']} 秒内必须有钩子", "第1镜")
    # 标题与标签
    if len(c.get("title", "")) > p["max_title_chars"]:
        add("block", "标题", f"标题 {len(c['title'])} 字，超过 {p['max_title_chars']} 字", "title")
    if len(c.get("tags", [])) > p["max_tags"]:
        add("block", "标签", f"话题标签 {len(c['tags'])} 个，超过 {p['max_tags']} 个", "tags")
    # 引用必须可追溯
    card_ids = {f["id"] for f in card}
    set_ids = {e["id"] for e in settings}
    if not c.get("fact_ids"):
        add("block", "事实溯源", "脚本没有标注引用了哪条事实")
    for fid in c.get("fact_ids", []):
        if fid not in card_ids:
            add("block", "事实溯源", f"引用的事实 {fid} 不在事实卡里")
    for sid in c.get("setting_ids", []):
        if sid not in set_ids:
            add("block", "设定溯源", f"引用的设定 {sid} 不在本次检索到的设定里")
    allowed_numbers = rules.numbers(fact_card_block(card) + settings_block(settings))
    shown = rules.numbers(T_RE.sub("", text))
    stray = sorted(n for n in shown if n not in allowed_numbers)
    if stray:
        add("block", "数字溯源", f"这些数字在事实卡和设定里都找不到：{stray}")
    # 合规词表
    for rule_name, hits in [("广告法极限词", rules.ad_law_hits(text)),
                            ("防沉迷", rules.anti_addiction_hits(text)),
                            ("诱导充值", rules.pay_inducement_hits(text)),
                            ("红线", rules.redline_hits(text))]:
        if hits:
            add("block", rule_name, "命中：" + "、".join(hits))
    for rule_name, hits in [("真人姓名", rules.real_person_hits(text)),
                            ("过往联动IP", rules.past_collab_ip_hits(text))]:
        if hits:
            add("legal", rule_name, "需法务会签：" + "、".join(hits))
    return issues


def judge(script: Script, key: str, card: list[dict], settings: list[dict], llm: LLM) -> dict:
    prompt = config.render("judge_script", game=config.game_setting()["game"],
                           script=json.dumps(script.content, ensure_ascii=False, indent=1),
                           facts=fact_card_block(card), settings=settings_block(settings))
    out = llm.complete_json("judge", key, prompt, role="judge")
    need = config.rules()["review"]["judge_pass_score"]
    out["passed"] = all(v >= need for v in out["scores"].values())
    return out


def review_loop(script: Script, ev_key: str, card: list[dict], settings: list[dict], llm: LLM,
                extra_issues: list[dict] | None = None) -> list[Script]:
    """返回本轮产生的所有版本（第一个是传入的版本）。最后一个版本的状态即结果。"""
    max_rounds = config.rules()["review"]["max_auto_revise_rounds"]
    versions = [script]
    current = script
    pending_human = list(extra_issues or [])
    for rnd in range(max_rounds + 1):
        check_transition(SCRIPT_TRANSITIONS, current.status, ScriptStatus.REVIEWING)
        current.status = ScriptStatus.REVIEWING
        key = f"{ev_key}:{current.platform}:v{current.version}"
        issues = rule_check(current.content, current.platform, card, settings)
        verdict = judge(current, key, card, settings, llm)
        current.reviews.append({"round": rnd, "rule_issues": issues, "judge": verdict,
                                "human_issues": pending_human})
        legal = [i for i in issues if i["severity"] == "legal"]
        current.flags = sorted({*current.flags, *(["legal_required"] if legal else [])})
        blocking = [i for i in issues if i["severity"] == "block"] + pending_human
        if not verdict["passed"]:
            blocking += [{"severity": "block", "rule": "模型评审", "where": i.get("where", ""),
                          "problem": i["problem"], "suggestion": i.get("suggestion", "")}
                         for i in verdict.get("issues", [])]
        if not blocking:
            current.status = ScriptStatus.AWAITING_FINAL
            break
        if rnd == max_rounds:
            current.status = ScriptStatus.NEEDS_HUMAN
            break
        current.status = ScriptStatus.REVISING
        prompt = config.render(
            "revise_script", game=config.game_setting()["game"],
            script=json.dumps(current.content, ensure_ascii=False, indent=1),
            issues=json.dumps(blocking, ensure_ascii=False, indent=1),
            fact_card=fact_card_block(card), settings=settings_block(settings))
        revised = llm.complete_json("revise", key, prompt, role="generator")
        current = replace(current, id=f"{current.event_id}-{current.platform}-v{current.version + 1}",
                          version=current.version + 1, content=revised, status=ScriptStatus.REVISING,
                          reviews=[], flags=list(current.flags))
        pending_human = []
        versions.append(current)
    return versions
