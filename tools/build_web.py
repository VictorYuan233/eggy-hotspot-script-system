"""把回放流水线的结果、规则、设定、提示词和评测集打包进网页 Demo。

    python tools/build_web.py   →  web/index.html
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.llm import ReplayLLM  # noqa: E402
from app.models import to_dict  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.store import Store  # noqa: E402

NOW = datetime.fromisoformat("2026-11-22T21:45:00+08:00")
seed = json.loads((ROOT / "data/seed_hotspots.json").read_text(encoding="utf-8"))
raw = [{**s, "fetched_at": seed["collected_at"]} for s in seed["snapshots"]]
r2 = json.loads((ROOT / "data/crawl_rss_2026-11-23.json").read_text(encoding="utf-8"))
NOW2 = datetime.fromisoformat(r2["fetched_at"])


def fresh(rounds=1):
    p = Pipeline(Store(), ReplayLLM())
    p.run_intake(raw, NOW, "manual")
    if rounds == 2:
        p.run_intake(r2["items"], NOW2, r2["round_id"])
    return p


# 1) 第一轮自动流水线跑完、人工还没操作时的状态
pipe = fresh()
state0 = pipe.export_state()
n_audit0 = len(state0["audit"])

# 2) 第二轮抓取：只记录“这一轮带来的变化”，由网页按当时的状态合并
p2 = fresh(2)
st2 = p2.export_state()
old_ids = {e["id"] for e in state0["events"]}
attach = {}
for e in st2["events"]:
    if e["id"] in old_ids:
        before = next(x for x in state0["events"] if x["id"] == e["id"])
        extra = [i for i in e["snapshot_ids"] if i not in before["snapshot_ids"]]
        if extra:
            attach[e["id"]] = extra
round2 = {
    "round_id": r2["round_id"], "fetched_at": r2["fetched_at"], "feed": r2["feed"], "note": r2["note"],
    "snapshots": [s for s in st2["snapshots"] if s["id"].startswith("C")],
    "events": [e for e in st2["events"] if e["id"] not in old_ids],
    "attach": attach,
    # 过期和补充来源由网页按当时的状态处理，这里只带其余的日志
    "audit": [a for a in st2["audit"][n_audit0:] if a["action"] not in ("expired", "attach_source")],
}

# 3) 运营确认后才生成的脚本（回放结果）
on_approve, contexts = {}, {}
for stage, p in ((1, pipe), (2, p2)):
    for ev in list(p.events.values()):
        if ev.status.value not in ("to_select", "not_recommended") or ev.fast_lane or "own_ops" in ev.flags:
            continue
        if stage == 2 and ev.id in old_ids:
            continue
        q = fresh(stage)
        try:
            scripts = q.approve_topic(ev.id, "运营", NOW2 if stage == 2 else NOW)
        except Exception:
            continue
        if scripts:
            on_approve[ev.id] = [to_dict(s) for s in scripts]
            contexts[ev.id] = q.events[ev.id].context

# 4) 回放模式下可演示的“退回改稿”
q = fresh()
xj = next(e for e in q.events.values() if e.key == "R04")
sc = q.approve_topic(xj.id, "运营", NOW)[-1]
versions = q.return_script(sc.id, "运营", "钩子不够抓人", NOW)
return_examples = {sc.id: {"comment": "钩子不够抓人", "versions": [to_dict(v) for v in versions[1:]],
                           "review0": to_dict(versions[0])["reviews"][-1]}}

state0["contexts"].update(contexts)
data = {
    "generatedAt": NOW.isoformat(),
    "seedNote": seed["note"],
    "rules": config.rules(),
    "setting": config.game_setting(),
    "sources": config.sources(),
    "prompts": {p.stem: p.read_text(encoding="utf-8") for p in sorted((ROOT / "prompts").glob("*.md"))},
    "evalSet": json.loads((ROOT / "data/eval_set.json").read_text(encoding="utf-8")),
    "state0": state0,
    "round2": round2,
    "onApprove": on_approve,
    "returnExamples": return_examples,
}
blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
tpl = (ROOT / "web/template.html").read_text(encoding="utf-8")
(ROOT / "web/index.html").write_text(tpl.replace("/*__DATA__*/null", blob), encoding="utf-8")
print("web/index.html", len(blob) // 1024, "KB;", "onApprove:", list(on_approve), "attach:", attach,
      "round2 events:", [e["id"] for e in round2["events"]])
