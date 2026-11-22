"""把回放流水线的结果、规则、设定和提示词打包进网页 Demo。

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

# 1) 自动流水线跑完、人工还没操作时的状态
pipe = Pipeline(Store(), ReplayLLM())
pipe.run_intake(raw, NOW)
state0 = pipe.export_state()

# 2) 非快速通道事件在运营确认后会生成的脚本（回放结果）
on_approve, contexts = {}, {}
for ev in list(pipe.events.values()):
    if ev.status.value in ("to_select", "not_recommended") and not ev.fast_lane and "own_ops" not in ev.flags:
        p2 = Pipeline(Store(), ReplayLLM())
        p2.run_intake(raw, NOW)
        try:
            scripts = p2.approve_topic(ev.id, "运营", NOW)
        except Exception:
            continue
        if scripts:
            on_approve[ev.id] = [to_dict(s) for s in scripts]
        contexts.update(p2.export_state()["contexts"])
state0["contexts"].update(contexts)

data = {
    "generatedAt": NOW.isoformat(),
    "seedNote": seed["note"],
    "rules": config.rules(),
    "setting": config.game_setting(),
    "sources": config.sources(),
    "prompts": {p.stem: p.read_text(encoding="utf-8") for p in sorted((ROOT / "prompts").glob("*.md"))},
    "state0": state0,
    "onApprove": on_approve,
}
blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
tpl = (ROOT / "web/template.html").read_text(encoding="utf-8")
(ROOT / "web/index.html").write_text(tpl.replace("/*__DATA__*/null", blob), encoding="utf-8")
print("web/index.html", len(blob) // 1024, "KB data;", "onApprove:", list(on_approve))
