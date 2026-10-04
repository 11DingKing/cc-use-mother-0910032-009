"""到期/宽限/失效判定与限制派生策略（纯函数）。

设计原则：

* 时钟由外部注入（:class:`Clock`），所有判定只依赖 ``today`` 与实体状态，
  因此同一状态下重复巡检得到完全相同的结果（幂等、可测试）。
* 风险项只有三类：``待补件`` / ``宽限`` / ``失效``；监管暂停不属于到期
  风险，但同样派生授权限制。
* 有效期按“当日仍有效”处理，``valid_to`` 的次日才构成失效。
* 到期时若续期案件已提交、正在等待核验，自动进入管理宽限期
  （默认 30 日，可配置），避免“已提交未核验”被简单按日期停用；
  临时凭证可在管理宽限之外延长保护窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from .models import (
    Qualification,
    RegulatorySuspension,
    RenewalCase,
    RenewalStatus,
    Restriction,
    RestrictionKind,
    RiskCategory,
    TemporaryPermit,
)

#: 到期后“续期已提交待核验”的默认管理宽限天数。
DEFAULT_GRACE_DAYS = 30


class Clock(Protocol):
    """可注入时钟。"""

    def today(self) -> date: ...


class FixedClock:
    """测试/回放用固定时钟。"""

    def __init__(self, today: date) -> None:
        self._today = today

    def today(self) -> date:
        return self._today

    def advance(self, days: int = 1) -> date:
        self._today += timedelta(days=days)
        return self._today


class SystemClock:
    """生产时钟，读取系统本地日期。"""

    def today(self) -> date:
        return date.today()


@dataclass(frozen=True)
class RiskDraft:
    """巡检策略输出的风险项草案。"""

    category: RiskCategory
    renewal_case_id: str | None
    reason: str
    supplement_due: date | None = None
    supplement_requested_at: date | None = None
    grace_deadline: date | None = None
    permit_id: str | None = None


def _active_case(cases: list[RenewalCase], today: date) -> RenewalCase | None:
    """最近一个仍在途（提交/待补件）的续期案件。"""
    in_flight = [
        c
        for c in cases
        if c.status in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING)
    ]
    if not in_flight:
        return None
    return max(in_flight, key=lambda c: c.submitted_at)


def _covering_permit(
    permits: list[TemporaryPermit], case: RenewalCase, today: date
) -> TemporaryPermit | None:
    """找出当天真正起作用的临时凭证：窗口有效且关联案件仍在等待核验。"""
    if case.status is not RenewalStatus.SUBMITTED:
        # 待补件阶段不享受宽限执业，先走“待补件”限制。
        return None
    candidates = [
        p
        for p in permits
        if p.renewal_case_id == case.id and p.is_window_valid(today)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.valid_to)


def evaluate_qualification(
    qualification: Qualification,
    cases: list[RenewalCase],
    permits: list[TemporaryPermit],
    today: date,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> RiskDraft | None:
    """按日期与案件状态判定单个资质的风险项；无风险返回 ``None``。

    判定优先级（高到低）：

    1. 待补件：案件处于 supplementing 且仍在补件期限内——即使尚未到期，
       先冻结新排班，避免带病排班。
    2. 宽限：已过期，但续期案件在等待核验——自动进入 ``grace_days`` 日
       管理宽限期；临时凭证在管理宽限之外可继续延长保护窗口。
    3. 失效：已过期且不存在有效宽限依据（无在途案件、案件终局、
       管理宽限与凭证均到期，或补件逾期）。
    补件逾期但资质尚未到期时仍列待补件（标注逾期），到期后转失效。
    """
    expiry = qualification.expiry_basis()
    case = _active_case(cases, today)

    if case is not None and case.status is RenewalStatus.SUPPLEMENTING:
        due = case.supplement_due
        overdue = due is not None and today > due
        if today <= expiry:
            label = "补件已逾期" if overdue else "待补件"
            return RiskDraft(
                category=RiskCategory.MISSING_DOCS,
                renewal_case_id=case.id,
                reason=(
                    f"{label}：续期案件 {case.id} 需于 {due} 前补齐材料，"
                    "补齐前冻结新排班，现有服务可继续"
                ),
                supplement_due=due,
                supplement_requested_at=case.supplement_requested_at,
            )
        # 已过期：补件期限内仍是“待补件”，逾期则不再保护、按失效处理。
        if due is not None and today <= due:
            return RiskDraft(
                category=RiskCategory.MISSING_DOCS,
                renewal_case_id=case.id,
                reason=(
                    f"待补件：资质已于 {expiry} 到期，案件 {case.id} 需于 "
                    f"{due} 前补齐材料，补齐前冻结新排班"
                ),
                supplement_due=due,
            )
        return RiskDraft(
            category=RiskCategory.EXPIRED,
            renewal_case_id=case.id,
            reason=(
                f"失效：资质已于 {expiry} 到期，案件 {case.id} 补件逾期（{due}）"
            ),
            supplement_due=due,
        )

    if today > expiry:
        permit = _covering_permit(permits, case, today) if case else None
        if permit is not None:
            return RiskDraft(
                category=RiskCategory.GRACE,
                renewal_case_id=case.id,
                reason=(
                    f"宽限：资质已于 {expiry} 到期，续期案件 {case.id} 在途，"
                    f"临时凭证 {permit.id} 覆盖至 {permit.valid_to}，"
                    "凭证范围内可继续执业"
                ),
                grace_deadline=permit.valid_to,
                permit_id=permit.id,
            )
        admin_deadline = expiry + timedelta(days=grace_days)
        if (
            case is not None
            and case.status is RenewalStatus.SUBMITTED
            and case.submitted_at <= expiry
            and today <= admin_deadline
        ):
            return RiskDraft(
                category=RiskCategory.GRACE,
                renewal_case_id=case.id,
                reason=(
                    f"宽限：资质已于 {expiry} 到期，续期案件 {case.id} 已于到期前"
                    f"提交待核验，管理宽限保护至 {admin_deadline}，"
                    "期间可在原授权范围内继续执业"
                ),
                grace_deadline=admin_deadline,
            )
        if (
            case is not None
            and case.status is RenewalStatus.SUBMITTED
            and today > admin_deadline
        ):
            return RiskDraft(
                category=RiskCategory.EXPIRED,
                renewal_case_id=case.id,
                reason=(
                    f"失效：资质已于 {expiry} 到期，在途案件 {case.id} 超过"
                    f"管理宽限期（{admin_deadline}）且无有效临时凭证，停止执业"
                ),
            )
        case_desc = f"，在途案件 {case.id} 无有效临时凭证" if case else "，无在途续期案件"
        return RiskDraft(
            category=RiskCategory.EXPIRED,
            renewal_case_id=case.id if case else None,
            reason=f"失效：资质已于 {expiry} 到期{case_desc}，停止执业",
        )

    return None


def expired_restriction_effective_from(
    qualification: Qualification,
    cases: list[RenewalCase],
    permits: list[TemporaryPermit],
    today: date,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> date:
    """计算“失效停业”限制的客观生效日。

    停业不是简单从到期次日起算——若人员客观上享有宽限保护（到期前提交
    续期、管理宽限期或临时凭证窗口），保护期内执业合法，停业自保护结束
    次日生效；续期被撤回/驳回的，自终局事件日与理论宽限截止日的较早者
    生效。这样既保留历史服务依据，也不允许终局后继续执业。
    """
    expiry = qualification.expiry_basis()
    default_from = expiry + timedelta(days=1)
    case = _active_case(cases, today)
    if case is None:
        # 取最近一个终局案件，判断其终局前是否享有宽限。
        terminal = [
            c
            for c in cases
            if c.status
            in (RenewalStatus.VERIFIED, RenewalStatus.REJECTED, RenewalStatus.WITHDRAWN)
        ]
        case = max(terminal, key=lambda c: c.submitted_at) if terminal else None
    if case is None:
        return default_from

    admin_deadline = (
        expiry + timedelta(days=grace_days)
        if case.submitted_at <= expiry
        else expiry
    )
    permit_end = max(
        (p.valid_to for p in permits if p.renewal_case_id == case.id),
        default=expiry,
    )

    if case.status is RenewalStatus.SUBMITTED:
        return max(admin_deadline, permit_end) + timedelta(days=1)

    if case.status is RenewalStatus.SUPPLEMENTING:
        # 过期后补件期限内允许执业；补件逾期的，停业自补件截止次日与
        # 到期次日的较晚者生效（补件截止可能早于到期日）。
        return max(default_from, (case.supplement_due or expiry) + timedelta(days=1))

    if case.status in (RenewalStatus.REJECTED, RenewalStatus.WITHDRAWN):
        # 终局即时丧失保护：停业不晚于驳回/撤回当日；但不得早于到期次日，
        # 因为有效期内的驳回/撤回本身不停止执业。
        ended_at = case.rejected_at or case.withdrawn_at or today
        nominal_from = max(admin_deadline, permit_end) + timedelta(days=1)
        return max(default_from, min(ended_at, nominal_from))

    return default_from


def active_suspension(
    suspensions: list[RegulatorySuspension], today: date
) -> RegulatorySuspension | None:
    actives = [s for s in suspensions if s.is_active(today)]
    if not actives:
        return None
    return max(actives, key=lambda s: s.effective_from)


def derive_restrictions(
    qualification: Qualification,
    risk: RiskDraft | None,
    suspensions: list[RegulatorySuspension],
    today: date,
    cases: list[RenewalCase] | None = None,
    permits: list[TemporaryPermit] | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> list[Restriction]:
    """根据风险项与监管暂停，派生该资质关联授权上“应当生效”的限制。

    返回的是期望值集合，服务层据此与授权上已存在的限制做对账（幂等）：
    已存在且生效时间一致的不重复写入；应当消失的限制补上结束日。
    """
    desired: list[Restriction] = []

    # 监管暂停（含已登记的未来生效/未来解除）都按已知窗口落账，
    # 这样暂停一旦登记即可传播到未来排班；解除日登记后窗口自动收口。
    for suspension in suspensions:
        end = (
            suspension.lift_effective_date - timedelta(days=1)
            if suspension.lift_effective_date is not None
            else None
        )
        desired.append(
            Restriction(
                kind=RestrictionKind.PRACTICE_SUSPENDED,
                source_type="regulatory_suspension",
                source_id=suspension.id,
                reason=f"监管暂停：{suspension.reason}",
                effective_from=suspension.effective_from,
                effective_to=end,
            )
        )

    if risk is not None:
        if risk.category is RiskCategory.MISSING_DOCS:
            case_id = risk.renewal_case_id or ""
            desired.append(
                Restriction(
                    kind=RestrictionKind.SCHEDULING_FROZEN,
                    source_type="renewal_case",
                    source_id=case_id,
                    reason=(
                        f"待补件：续期案件 {case_id} 材料不齐"
                        + (
                            f"，需于 {risk.supplement_due} 前补齐"
                            if risk.supplement_due
                            else ""
                        )
                    ),
                    # 自补件要求提出当日起冻结新排班。
                    effective_from=risk.supplement_requested_at or today,
                )
            )
        elif risk.category is RiskCategory.EXPIRED:
            desired.append(
                Restriction(
                    kind=RestrictionKind.PRACTICE_SUSPENDED,
                    source_type="qualification",
                    source_id=qualification.id,
                    reason=risk.reason,
                    # 依据案件与凭证客观计算：宽限/补件保护结束次日才停业，
                    # 因此不会改写保护期内的历史服务结论。
                    effective_from=expired_restriction_effective_from(
                        qualification,
                        cases or [],
                        permits or [],
                        today,
                        grace_days,
                    ),
                )
            )
        # 宽限本身不新增限制：临时凭证窗口内允许在原授权范围内执业；
        # 超过凭证窗口的未来排班由排班传播规则取消。

    return desired


def restriction_effective_explanation(restriction: Restriction, today: date) -> str:
    """接口使用：用中文说明每项限制何时生效、何时解除。"""
    end = (
        restriction.effective_to.isoformat()
        if restriction.effective_to is not None
        else "解除事件登记前持续有效"
    )
    active = "当前生效中" if restriction.is_active(today) else "当前未生效"
    if restriction.kind is RestrictionKind.SCHEDULING_FROZEN:
        scope = "仅冻结新排班（含未来候选排班），不影响当日及历史服务"
    else:
        scope = "停止一切执业，取消覆盖期内未来排班，历史服务记录保留"
    return (
        f"{restriction.reason}；生效日 {restriction.effective_from.isoformat()}（含当日），"
        f"至 {end}；{scope}；状态：{active}"
    )
