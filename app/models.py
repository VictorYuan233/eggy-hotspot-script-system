"""数据模型与状态机。

对应 C1 方案里“需要保存的数据”和“关键状态”两节。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventStatus(str, Enum):
    NEW = "new"                    # 新发现
    VERIFYING = "verifying"        # 核验中
    VERIFIED = "verified"          # 已核验
    PENDING = "pending"            # 待核实（来源不足）
    DOUBTFUL = "doubtful"          # 存疑
    REJECTED = "rejected"          # 驳回（红线、辟谣）
    TO_SELECT = "to_select"        # 待选题（已进入候选看板）
    NOT_RECOMMENDED = "not_recommended"  # 已评估，不推荐跟进
    SELECTED = "selected"          # 运营已选
    DECLINED = "declined"          # 运营已拒
    EXPIRED = "expired"            # 已过期，自动下线


EVENT_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.NEW: {EventStatus.VERIFYING, EventStatus.REJECTED, EventStatus.EXPIRED,
                      EventStatus.NOT_RECOMMENDED},
    EventStatus.VERIFYING: {EventStatus.VERIFIED, EventStatus.PENDING, EventStatus.DOUBTFUL,
                            EventStatus.REJECTED},
    EventStatus.VERIFIED: {EventStatus.TO_SELECT, EventStatus.NOT_RECOMMENDED,
                           EventStatus.REJECTED, EventStatus.EXPIRED},
    EventStatus.PENDING: {EventStatus.VERIFYING, EventStatus.EXPIRED},
    EventStatus.DOUBTFUL: {EventStatus.VERIFYING, EventStatus.EXPIRED},
    EventStatus.TO_SELECT: {EventStatus.SELECTED, EventStatus.DECLINED, EventStatus.EXPIRED},
    EventStatus.NOT_RECOMMENDED: {EventStatus.SELECTED, EventStatus.EXPIRED},
    EventStatus.SELECTED: set(),
    EventStatus.DECLINED: set(),
    EventStatus.REJECTED: set(),
    EventStatus.EXPIRED: set(),
}


class ScriptStatus(str, Enum):
    PREGENERATED = "pregenerated"  # 预生成草稿（仅快速通道）
    DRAFT = "draft"                # 草稿
    REVIEWING = "reviewing"        # 自动评审中
    REVISING = "revising"          # 自动修改中
    NEEDS_HUMAN = "needs_human"    # 自动修改超过轮次，标红待人工
    AWAITING_FINAL = "awaiting_final"  # 待终审
    RETURNED = "returned"          # 退回修改
    APPROVED = "approved"          # 已通过
    PUBLISHED = "published"        # 已发布（人工操作后回填）
    RETRO_DONE = "retro_done"      # 已复盘


SCRIPT_TRANSITIONS: dict[ScriptStatus, set[ScriptStatus]] = {
    ScriptStatus.PREGENERATED: {ScriptStatus.REVIEWING},
    ScriptStatus.DRAFT: {ScriptStatus.REVIEWING},
    ScriptStatus.REVIEWING: {ScriptStatus.REVISING, ScriptStatus.AWAITING_FINAL,
                             ScriptStatus.NEEDS_HUMAN},
    ScriptStatus.REVISING: {ScriptStatus.REVIEWING},
    ScriptStatus.NEEDS_HUMAN: {ScriptStatus.REVIEWING, ScriptStatus.AWAITING_FINAL},
    ScriptStatus.AWAITING_FINAL: {ScriptStatus.APPROVED, ScriptStatus.RETURNED},
    ScriptStatus.RETURNED: {ScriptStatus.REVIEWING},
    ScriptStatus.APPROVED: {ScriptStatus.PUBLISHED},
    ScriptStatus.PUBLISHED: {ScriptStatus.RETRO_DONE},
    ScriptStatus.RETRO_DONE: set(),
}


class TransitionError(ValueError):
    pass


def check_transition(table: dict, current: Enum, target: Enum) -> None:
    if target not in table[current]:
        raise TransitionError(f"不允许的状态流转：{current.value} → {target.value}")


@dataclass
class Snapshot:
    """原始快照：写入后不可修改。"""
    id: str
    source_id: str
    title: str
    url: str
    publisher: str
    platform: str
    published_at: str
    fetched_at: str
    summary: str
    heat: float = 0
    heat_series: list[float] = field(default_factory=list)
    heat_simulated: bool = True


@dataclass
class Fact:
    id: str
    text: str
    source_ids: list[str]


@dataclass
class Event:
    id: str
    key: str                          # 稳定键：簇内最早录入的快照 ID
    title: str
    snapshot_ids: list[str]
    type: str = "unknown"             # news / meme
    type_reason: str = ""
    status: EventStatus = EventStatus.NEW
    status_reason: str = ""
    expire_at: str = ""
    urgency: str = "normal"           # high / normal
    facts: list[Fact] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)   # 例如 legal_required、own_ops
    score: dict[str, Any] = field(default_factory=dict)
    fast_lane: bool = False


@dataclass
class Script:
    id: str
    event_id: str
    platform: str
    version: int
    content: dict[str, Any]
    status: ScriptStatus = ScriptStatus.DRAFT
    pregenerated: bool = False
    setting_version: str = ""
    prompt_version: str = ""
    reviews: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class AuditEntry:
    at: str
    actor: str          # system / llm / 运营 / 法务 ...
    action: str
    target: str
    detail: str = ""


def to_dict(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    return obj


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
