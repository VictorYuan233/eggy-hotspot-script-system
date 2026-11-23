"""⑩ 数据回流：根据运营的拒绝理由、退回意见和发布效果，生成参数调整建议。

建议只给人看，由运营负责人确认后再改配置，系统不会自动改权重。
"""
from __future__ import annotations

from collections import Counter

from . import config


def suggestions(audit: list[dict], min_count: int = 2) -> list[dict]:
    rules = config.rules()["feedback_rules"]
    declines = Counter(a["detail"] for a in audit if a["action"] == "decline_topic")
    returns = [a for a in audit if a["action"] == "return_script"]
    out = []
    for reason, n in declines.most_common():
        rule = rules.get(reason, rules["其他"])
        out.append({"signal": f"拒绝理由“{reason}”出现 {n} 次", "suggest": rule["suggest"],
                    "param": rule["param"], "ready": n >= min_count})
    if returns:
        out.append({"signal": f"终审退回 {len(returns)} 次",
                    "suggest": "把退回意见加入生成提示词的“常见问题”清单，并加入评测集",
                    "param": "prompts/generate_script.md", "ready": len(returns) >= min_count})
    return out
