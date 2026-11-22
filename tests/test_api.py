import os

os.environ["EGGY_NOW"] = "2026-11-22T21:45:00+08:00"

from fastapi.testclient import TestClient  # noqa: E402

from app.api import app  # noqa: E402


def test_api_flow():
    c = TestClient(app)
    events = c.post("/intake/seed").json()
    xs = next(e for e in events if e["key"] == "R01")
    assert xs["fast_lane"]
    # 未确认选题就终审：403
    assert c.post(f"/scripts/{xs['id']}-douyin-v2/approve", json={"operator": "运营"}).status_code == 403
    assert c.post(f"/events/{xs['id']}/approve", json={"operator": "运营"}).status_code == 200
    r = c.post(f"/scripts/{xs['id']}-douyin-v2/approve", json={"operator": "运营"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # 拒绝理由不在固定选项里：409
    xj = next(e for e in events if e["key"] == "R04")
    assert c.post(f"/events/{xj['id']}/decline", json={"operator": "运营", "reason": "不想做"}).status_code == 409
    assert any(a["action"] == "final_approve" for a in c.get("/audit").json())
