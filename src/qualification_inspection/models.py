"""领域模型：资质、续期案件、临时凭证、监管暂停、授权与排班。

时间一律使用本地时区的 ``date``（有效期到当天结束），时钟由外部注入，
所有派生状态都是纯函数，保证巡检可重复、结果确定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class QualificationStatus(str, Enum):
    """资质生命周期状态。"""

    ACTIVE = "登记"                 # 有效期内
    PENDING_VERIFICATION = "待核验"  # 续期/补证案件已提交，等待核验
    DISPOSED = "处置中"             # 风险待办已形成，需要处置
    DECIDED = "已决定"              # 处置终局：完成、驳回或失效
    ARCHIVED = "已归档"             # 历史归档，只读


class RenewalStatus(str, Enum):
    """续期案件状态。"""

    SUBMITTED = "submitted"        # 已提交，等待核验
    SUPPLEMENTING = "supplementing"  # 待补件：核验后材料不齐，限期补齐
    VERIFIED = "verified"          # 核验通过（决定资质的终局事实）
    REJECTED = "rejected"          # 核验驳回
    WITHDRAWN = "withdrawn"        # 申请人撤回


class RiskCategory(str, Enum):
    """巡检风险项类别。"""

    MISSING_DOCS = "待补件"   # 续期案件待补件：限制新排班，现有服务可继续
    GRACE = "宽限"           # 已过期但宽限期内，且续期在途
    EXPIRED = "失效"         # 过期且无有效宽限依据


class RestrictionKind(str, Enum):
    """授权限制种类。生效规则见模块文档“限制何时生效”。"""

    SCHEDULING_FROZEN = "scheduling_frozen"  # 待补件：仅冻结新排班
    PRACTICE_SUSPENDED = "practice_suspended"  # 失效/监管暂停：停止一切执业


@dataclass
class Restriction:
    """一项授权限制及其生效时间窗口。

    可变：巡检对账时若限制提前解除，会回写 ``effective_to`` 收口；
    已落账的历史窗口因此保留，可还原历史服务当日的限制状态。
    """

    kind: RestrictionKind
    source_type: str          # qualification | renewal_case | regulatory_suspension
    source_id: str
    reason: str
    effective_from: date      # 含当日
    effective_to: date | None = None  # None 表示在事件撤销/资质失效前持续

    def is_active(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on <= self.effective_to


@dataclass
class Qualification:
    """人员资质（执业证 / 主诊资格）。

    ``valid_to`` 为有效期截止日（当日仍有效）。``new_valid_to`` 是核验通过
    后写入的新有效期；若未通过核验，原有效期不因提交续期而改变。
    """

    id: str
    person_id: str
    qual_type: str
    valid_from: date
    valid_to: date
    status: QualificationStatus = QualificationStatus.ACTIVE
    new_valid_to: date | None = None
    decided_at: date | None = None
    archived_at: date | None = None

    def expiry_basis(self) -> date:
        """当前据以判断到期的有效期：优先核验通过的新有效期。"""
        return self.new_valid_to or self.valid_to


@dataclass
class RenewalDocument:
    """续期材料。"""

    doc_id: str
    name: str
    present: bool = False


@dataclass
class RenewalCase:
    """续期案件：提交、补件、核验、撤回的状态机载体。"""

    id: str
    qualification_id: str
    submitted_at: date
    status: RenewalStatus = RenewalStatus.SUBMITTED
    documents: list[RenewalDocument] = field(default_factory=list)
    supplement_due: date | None = None      # 补件截止日
    supplement_requested_at: date | None = None
    verified_at: date | None = None
    decided_valid_to: date | None = None    # 核验通过认定的新有效期
    rejected_at: date | None = None
    reject_reason: str | None = None
    withdrawn_at: date | None = None

    def missing_documents(self) -> list[str]:
        return [d.name for d in self.documents if not d.present]


@dataclass
class TemporaryPermit:
    """临时凭证：宽限期内允许在现有授权范围内继续执业。

    凭证有独立的有效期窗口，且必须关联一个在途续期案件；案件终局
    （撤回/驳回）后凭证立即失效，与 ``valid_to`` 是否到期无关。
    """

    id: str
    qualification_id: str
    renewal_case_id: str
    valid_from: date
    valid_to: date
    scope_note: str = "宽限期临时执业"

    def is_window_valid(self, on: date) -> bool:
        return self.valid_from <= on <= self.valid_to


@dataclass
class RegulatorySuspension:
    """监管暂停指令：来自监管部门，立即生效，可解除。"""

    id: str
    qualification_id: str
    effective_from: date
    reason: str
    lifted_at: date | None = None
    lift_effective_date: date | None = None

    def is_active(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        if self.lift_effective_date is not None:
            return on < self.lift_effective_date
        return self.lifted_at is None  # 未登记解除则持续


@dataclass
class Authorization:
    """资质关联授权（可开展的服务项目）。

    授权本身不被删除，只叠加 :class:`Restriction`；历史服务记录因此始终
    能追溯到当时有效的授权依据。
    """

    id: str
    qualification_id: str
    person_id: str
    service_code: str
    service_name: str
    granted_from: date
    granted_to: date | None = None
    restrictions: list[Restriction] = field(default_factory=list)

    def active_restrictions(self, on: date) -> list[Restriction]:
        return [r for r in self.restrictions if r.is_active(on)]

    def can_practice(self, on: date) -> bool:
        """当日是否可实际执业：授权在窗口内且无执业暂停类限制。"""
        if on < self.granted_from:
            return False
        if self.granted_to is not None and on > self.granted_to:
            return False
        return not any(
            r.kind is RestrictionKind.PRACTICE_SUSPENDED
            for r in self.active_restrictions(on)
        )

    def can_be_scheduled(self, on: date) -> bool:
        """当日是否可排入新班：执业可行且无任何活动限制（含仅冻结排班）。"""
        return self.can_practice(on) and not self.active_restrictions(on)


@dataclass
class ServiceRecord:
    """历史服务记录（不可变），作为“历史服务依据”留存。"""

    id: str
    authorization_id: str
    person_id: str
    service_date: date
    service_name: str


@dataclass
class ScheduleEntry:
    """排班条目。"""

    id: str
    authorization_id: str
    person_id: str
    service_code: str
    scheduled_on: date
    # pending：未来候选排班；confirmed：已确认；cancelled：因限制传播取消
    status: str = "pending"
    cancel_reason: str | None = None
    cancelled_at: date | None = None
