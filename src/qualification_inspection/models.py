"""领域模型。

覆盖：人员、资质（执业证/主诊资格等）、续期案件、临时凭证、授权、
监管暂停、风险项、授权限制、历史服务记录与巡检运行记录。

所有日期字段使用 ``datetime.date``；嵌套事件列表中统一保存 ISO 字符串，
由服务层在需要时解析。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, ClassVar

# ── 资质类型 ─────────────────────────────────────────────────────────────
PRACTICE_LICENSE = "PRACTICE_LICENSE"
"""执业证。"""
ATTENDING_QUALIFICATION = "ATTENDING_QUALIFICATION"
"""主诊资格。"""

# ── 续期案件状态（映射契约五态）────────────────────────────────────────────
RENEWAL_SUBMITTED = "SUBMITTED"
"""已提交，等待核验（契约态：待核验）。"""
RENEWAL_SUPPLEMENT_REQUESTED = "SUPPLEMENT_REQUESTED"
"""核验中要求补件（契约态：处置中：待补件）。"""
RENEWAL_SUPPLEMENTED = "SUPPLEMENTED"
"""补证完成，重新待核验（契约态：处置中）。"""
RENEWAL_APPROVED = "APPROVED"
"""续期核准（契约态：已决定）。"""
RENEWAL_REJECTED = "REJECTED"
"""续期驳回（契约态：已决定）。"""
RENEWAL_WITHDRAWN = "WITHDRAWN"
"""申请人撤回（契约态：已归档）。"""

RENEWAL_PENDING = {
    RENEWAL_SUBMITTED,
    RENEWAL_SUPPLEMENT_REQUESTED,
    RENEWAL_SUPPLEMENTED,
}
RENEWAL_TERMINAL = {RENEWAL_APPROVED, RENEWAL_REJECTED, RENEWAL_WITHDRAWN}

# 案件状态 → 契约态
CONTRACT_PHASE = {
    RENEWAL_SUBMITTED: "待核验",
    RENEWAL_SUPPLEMENT_REQUESTED: "处置中",
    RENEWAL_SUPPLEMENTED: "处置中",
    RENEWAL_APPROVED: "已决定",
    RENEWAL_REJECTED: "已决定",
    RENEWAL_WITHDRAWN: "已归档",
}

# ── 风险类别 ─────────────────────────────────────────────────────────────
RISK_EXPIRING_SOON = "EXPIRING_SOON"
"""临期提醒（提示，不阻断）。"""
RISK_SUPPLEMENT_DUE = "SUPPLEMENT_DUE"
"""待补件（逾期未补则升级为硬限制）。"""
RISK_GRACE = "GRACE"
"""宽限（已过期但续期在途，提示不阻断）。"""
RISK_LAPSED = "LAPSED"
"""失效（硬限制）。"""
RISK_SUSPENDED = "SUSPENDED"
"""监管暂停（硬限制，即时生效）。"""
RISK_CLEARED = "CLEARED"
"""风险消除（仅用于巡检归档，不再产生限制）。"""

RISK_TITLES = {
    RISK_EXPIRING_SOON: "临期提醒",
    RISK_SUPPLEMENT_DUE: "待补件",
    RISK_GRACE: "宽限",
    RISK_LAPSED: "失效",
    RISK_SUSPENDED: "监管暂停",
    RISK_CLEARED: "已消除",
}

# ── 限制级别与类型 ────────────────────────────────────────────────────────
LEVEL_BLOCK = "BLOCK"
"""硬限制：阻断未来排班。"""
LEVEL_WARN = "WARN"
"""软限制：允许排班但给出提示。"""

RESTRICTION_SUSPENSION = "REGULATORY_SUSPENSION"
"""监管暂停。"""
RESTRICTION_SUPPLEMENT_HOLD = "SUPPLEMENT_HOLD"
"""补件逾期冻结。"""
RESTRICTION_LAPSED = "LAPSED"
"""资质失效。"""
RESTRICTION_GRACE = "GRACE_WINDOW"
"""续期宽限提示。"""
RESTRICTION_EXPIRING = "EXPIRING_SOON"
"""临期提示。"""

# 由巡检风险项派生的限制（监管暂停限制由暂停命令直接管理）
DERIVED_RESTRICTION_TYPES = {
    RESTRICTION_SUPPLEMENT_HOLD,
    RESTRICTION_LAPSED,
    RESTRICTION_GRACE,
    RESTRICTION_EXPIRING,
}


def _convert(value: Any) -> Any:
    """把 dataclass/date 结构递归转换为可 JSON 化的值。"""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v) for v in value]
    return value


@dataclass
class Entity:
    """带日期字段处理的实体基类。"""

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entity":
        kwargs = dict(data)
        for name in cls.DATE_FIELDS:
            if kwargs.get(name) is not None:
                kwargs[name] = date.fromisoformat(kwargs[name])
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return _convert(asdict(self))


@dataclass
class Person(Entity):
    person_id: str
    name: str


@dataclass
class Qualification(Entity):
    qual_id: str
    person_id: str
    qual_type: str
    qual_number: str
    issued_on: date
    expiry_on: date

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("issued_on", "expiry_on")


@dataclass
class Authorization(Entity):
    """人员凭某项资质获得的授权（如门诊出诊、主诊诊疗）。"""

    authorization_id: str
    person_id: str
    qual_id: str
    scope: str
    granted_on: date
    revoked_on: date | None = None

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("granted_on", "revoked_on")


@dataclass
class RenewalCase(Entity):
    case_id: str
    qual_id: str
    submitted_on: date
    status: str = RENEWAL_SUBMITTED
    # 最近一轮补件要求
    supplement_requested_on: date | None = None
    supplement_deadline: date | None = None
    supplement_items: list[str] = field(default_factory=list)
    # 历次补件要求 [{requested_on, deadline, items}]
    supplement_rounds: list[dict[str, Any]] = field(default_factory=list)
    # 已补交材料 [{name, submitted_on}]
    docs: list[dict[str, Any]] = field(default_factory=list)
    decided_on: date | None = None
    new_expiry_on: date | None = None
    reject_reason: str | None = None
    withdrawn_on: date | None = None

    DATE_FIELDS: ClassVar[tuple[str, ...]] = (
        "submitted_on",
        "supplement_requested_on",
        "supplement_deadline",
        "decided_on",
        "new_expiry_on",
        "withdrawn_on",
    )

    def view(self) -> dict[str, Any]:
        data = self.to_dict()
        data["contract_phase"] = CONTRACT_PHASE[self.status]
        return data


@dataclass
class TemporaryCredential(Entity):
    """续期核验期间签发的临时凭证，在有效期内提供宽限覆盖。"""

    cred_id: str
    case_id: str
    qual_id: str
    valid_from: date
    valid_to: date
    issued_on: date
    voided_on: date | None = None
    void_reason: str | None = None

    DATE_FIELDS: ClassVar[tuple[str, ...]] = (
        "valid_from",
        "valid_to",
        "issued_on",
        "voided_on",
    )

    def covers(self, day: date) -> bool:
        return self.voided_on is None and self.valid_from <= day <= self.valid_to


@dataclass
class Suspension(Entity):
    """监管暂停文书。"""

    order_no: str
    person_id: str
    reason: str
    start_on: date
    end_on: date | None = None
    lifted_on: date | None = None
    # 限定授权范围；None 表示该人员全部授权
    scope: str | None = None

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("start_on", "end_on", "lifted_on")

    def active_on(self, day: date) -> bool:
        if day < self.start_on or self.lifted_on is not None:
            return False
        return self.end_on is None or day <= self.end_on


@dataclass
class Restriction(Entity):
    """授权限制。``effective_from``（含）起对未来排班生效。

    历史服务记录不随限制变化而失效（见 ``ServiceRecord``）。
    """

    restriction_id: str
    person_id: str
    restriction_type: str
    level: str
    title: str
    effective_from: date
    source: str
    reason: str
    # qual_id 为 None 表示人员级限制（如监管暂停）
    qual_id: str | None = None
    scope: str | None = None
    effective_to: date | None = None
    active: bool = True

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("effective_from", "effective_to")

    def applies_to(self, service_on: date) -> bool:
        if not self.active:
            return False
        if service_on < self.effective_from:
            return False
        return self.effective_to is None or service_on <= self.effective_to


@dataclass
class RiskItem(Entity):
    """月度巡检风险项，键 (period, qual_id) 幂等。"""

    risk_id: str
    period: str
    qual_id: str
    person_id: str
    category: str
    level: str
    title: str
    detail: str
    first_found_on: date
    last_seen_on: date
    effective_from: date
    hardens_on: date | None = None
    resolved_on: date | None = None
    # 类别迁移轨迹 [{from, to, on}]
    transitions: list[dict[str, Any]] = field(default_factory=list)

    DATE_FIELDS: ClassVar[tuple[str, ...]] = (
        "first_found_on",
        "last_seen_on",
        "effective_from",
        "hardens_on",
        "resolved_on",
    )


@dataclass
class ServiceRecord(Entity):
    """已发生服务的不可变依据快照；资质事后受限不影响其证据效力。"""

    record_id: str
    person_id: str
    authorization_id: str
    scope: str
    service_on: date
    qual_type: str
    qual_number: str
    qual_expiry_on: date
    detail: str = ""

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("service_on", "qual_expiry_on")


@dataclass
class InspectionRun(Entity):
    period: str
    ran_on: date
    risk_total: int = 0
    created: int = 0
    updated: int = 0
    cleared: int = 0
    stable: int = 0

    DATE_FIELDS: ClassVar[tuple[str, ...]] = ("ran_on",)
