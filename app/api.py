"""HTTP 接口（FastAPI）。运营看板通过这些接口完成选题确认、终审和退回。

    uvicorn app.api:app --reload
环境变量 EGGY_LIVE=1 时实时调用 Claude，否则用回放数据演示。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import feedback
from .collector import SourceNotAuthorized
from .config import DATA_DIR
from .llm import AnthropicLLM, ReplayLLM
from .models import TransitionError, to_dict
from .pipeline import DECLINE_REASONS, HumanGateError, Pipeline
from .store import Store

CST = timezone(timedelta(hours=8))
app = FastAPI(title="蛋仔派对热点营销脚本系统")
pipe = Pipeline(Store(os.environ.get("EGGY_DB", ":memory:")),
                AnthropicLLM() if os.environ.get("EGGY_LIVE") == "1" else ReplayLLM())


def now() -> datetime:
    fixed = os.environ.get("EGGY_NOW")   # 演示时固定时间，保证回放结果一致
    return datetime.fromisoformat(fixed) if fixed else datetime.now(CST)


def guard(fn):
    try:
        return fn()
    except (HumanGateError, SourceNotAuthorized) as exc:
        raise HTTPException(403, str(exc)) from exc
    except (TransitionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, f"找不到 {exc}") from exc


class SubmitLink(BaseModel):
    id: str
    title: str
    url: str
    summary: str
    submitted_by: str


class Decision(BaseModel):
    operator: str
    reason: str | None = None
    comment: str | None = None
    legal_signoff_by: str | None = None
    platforms: list[str] | None = None


@app.post("/intake/seed")
def intake_seed():
    seed = json.loads((DATA_DIR / "seed_hotspots.json").read_text(encoding="utf-8"))
    raw = [{**s, "fetched_at": seed["collected_at"]} for s in seed["snapshots"]]
    return [to_dict(e) for e in guard(lambda: pipe.run_intake(raw, now(), "manual"))]


@app.post("/crawl/run")
def crawl_run():
    """立即执行一轮定时采集（拉取授权清单里配置的 RSS）。"""
    from .scheduler import run_once
    return guard(lambda: run_once(pipe, now()))


@app.get("/feedback")
def feedback_suggestions():
    return feedback.suggestions(pipe.store.audit())


@app.post("/intake/submit")
def submit(link: SubmitLink):
    item = {"id": link.id, "source_id": "manual_submit", "title": link.title, "url": link.url,
            "publisher": f"运营提交：{link.submitted_by}", "platform": "manual",
            "published_at": now().isoformat(), "summary": link.summary}
    return [to_dict(e) for e in guard(lambda: pipe.run_intake([item], now(), f"submit-{link.id}"))]


@app.get("/events")
def events():
    return [to_dict(e) for e in pipe.events.values()]


@app.get("/scripts")
def scripts(event_id: str | None = None):
    return [to_dict(s) for s in pipe.scripts.values() if not event_id or s.event_id == event_id]


@app.get("/decline-reasons")
def decline_reasons():
    return DECLINE_REASONS


@app.post("/events/{event_id}/approve")
def approve(event_id: str, d: Decision):
    return [to_dict(s) for s in guard(lambda: pipe.approve_topic(event_id, d.operator, now(), d.platforms))]


@app.post("/events/{event_id}/decline")
def decline(event_id: str, d: Decision):
    guard(lambda: pipe.decline_topic(event_id, d.operator, d.reason or "", now()))
    return {"ok": True}


@app.post("/scripts/{script_id}/approve")
def final(script_id: str, d: Decision):
    return to_dict(guard(lambda: pipe.final_approve(script_id, d.operator, now(), d.legal_signoff_by)))


@app.post("/scripts/{script_id}/return")
def give_back(script_id: str, d: Decision):
    return [to_dict(s) for s in guard(lambda: pipe.return_script(script_id, d.operator, d.comment or "", now()))]


@app.get("/audit")
def audit():
    return pipe.store.audit()
