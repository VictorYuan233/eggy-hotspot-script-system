"""离线演示：用真实种子热点 + 预生成的模型输出，跑通从入库到终审的全流程。

    python -m app.demo            # 回放模式，不需要 API key
    python -m app.demo --live     # 实时调用 Claude（需要 ANTHROPIC_API_KEY）
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from .config import DATA_DIR
from .llm import AnthropicLLM, ReplayLLM
from .pipeline import HumanGateError, Pipeline
from .store import Store

NOW = datetime.fromisoformat("2026-11-22T21:45:00+08:00")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="实时调用 Claude API")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--out", default=str(DATA_DIR / "demo_state.json"))
    args = ap.parse_args()

    llm = AnthropicLLM() if args.live else ReplayLLM()
    pipe = Pipeline(Store(args.db), llm)
    seed = json.loads((DATA_DIR / "seed_hotspots.json").read_text(encoding="utf-8"))
    raw = [{**s, "fetched_at": seed["collected_at"]} for s in seed["snapshots"]]

    events = pipe.run_intake(raw, NOW)
    print(f"入库 {len(raw)} 条，归并成 {len(events)} 个事件：")
    for ev in events:
        score = f" 总分 {ev.score['total']}" if ev.score else ""
        lane = " [快速通道]" if ev.fast_lane else ""
        print(f"  {ev.id} {ev.status.value:<16}{score}{lane}  {ev.title[:24]}  —— {ev.status_reason}")

    by_key = {e.key: e for e in events}
    t = NOW
    # 运营打开看板：快速通道的小雪草稿已经就绪，先确认选题，再终审
    t += timedelta(minutes=5)
    xs = by_key["R01"]
    pipe.approve_topic(xs.id, "运营·小王", t)
    t += timedelta(minutes=3)
    for sc in [s for s in pipe.scripts.values() if s.event_id == xs.id and s.status.value == "awaiting_final"]:
        pipe.final_approve(sc.id, "运营·小王", t)
    # 雪假：正常通道，确认后才生成
    t += timedelta(minutes=5)
    xj = by_key["R04"]
    produced = pipe.approve_topic(xj.id, "运营·小王", t)
    t += timedelta(minutes=4)
    pipe.final_approve(produced[-1].id, "运营·小王", t)
    # 厉娜体：涉及真人，没有法务会签不能通过
    t += timedelta(minutes=5)
    ln = by_key["R07"]
    produced = pipe.approve_topic(ln.id, "运营·小王", t)
    try:
        pipe.final_approve(produced[-1].id, "运营·小王", t)
    except HumanGateError as exc:
        print(f"\n终审被拦下：{exc}")

    print("\n脚本：")
    for sc in pipe.scripts.values():
        flags = f" {sc.flags}" if sc.flags else ""
        print(f"  {sc.id:<26}{sc.status.value:<16}{'预生成' if sc.pregenerated else ''}{flags}")

    state = pipe.export_state()
    state["generated_at"] = NOW.isoformat()
    state["mode"] = "live" if args.live else "replay"
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print(f"\n审计日志 {len(state['audit'])} 条，状态已导出到 {args.out}")


if __name__ == "__main__":
    main()
