"""跑评测集：规则对坏样本的拦截率、对好样本的误拦率。

    python tools/eval.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, rules  # noqa: E402
from app.reviewer import rule_check  # noqa: E402


def classify_hotspot(text: str) -> str:
    if rules.redline_hits(text):
        return "红线"
    if rules.debunk_match(text):
        return "辟谣"
    if rules.own_ops_event(text):
        return "自家运营"
    return "pass"


def run() -> dict:
    ev = json.loads((ROOT / "data/eval_set.json").read_text(encoding="utf-8"))
    settings = [e for e in config.game_setting()["entries"] if e["id"] in ev["setting_ids"]]
    rows = []
    for case in ev["scripts"]:
        issues = rule_check(case["content"], "douyin", ev["fact_card"], settings)
        blocks = {i["rule"] for i in issues if i["severity"] == "block"}
        legal = any(i["severity"] == "legal" for i in issues)
        exp = case["expect"]
        ok = (not blocks and not legal) if exp == "pass" else (legal and not blocks) if exp == "legal" else exp in blocks
        rows.append({"kind": "脚本", "name": case["name"], "expect": exp,
                     "got": "、".join(sorted(blocks)) or ("法务" if legal else "通过"), "ok": ok})
    for case in ev["hotspots"]:
        got = classify_hotspot(case["text"])
        rows.append({"kind": "热点", "name": case["name"], "expect": case["expect"], "got": got,
                     "ok": got == case["expect"]})
    bad = [r for r in rows if r["expect"] != "pass"]
    good = [r for r in rows if r["expect"] == "pass"]
    return {"rows": rows,
            "catch_rate": sum(r["ok"] for r in bad) / len(bad),
            "false_block_rate": sum(not r["ok"] for r in good) / len(good)}


if __name__ == "__main__":
    res = run()
    for r in res["rows"]:
        print(("✓" if r["ok"] else "✗"), r["kind"], r["name"], "期望", r["expect"], "实际", r["got"])
    print(f"坏样本拦截率 {res['catch_rate']:.0%}，好样本误拦率 {res['false_block_rate']:.0%}")
