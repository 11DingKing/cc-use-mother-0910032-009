"""应用服务层：状态机命令、幂等巡检、限制对账与排班传播。

所有写操作只改实体状态；风险项与授权限制是派生数据，由 ``run_inspection``
按注入时钟统一对账。重复巡检同一天不会产生重复风险项或重复限制。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import ComplianceError, NotFoundError
from .models import (
    Authorization,
    Qualification,
    QualificationStatus,
    RegulatorySuspension,
    RenewalCase,
    RenewalDocument,
    RenewalStatus,
    Restriction,
    RestrictionKind,
    RiskCategory,
    ScheduleEntry,
    ServiceRecord,
    TemporaryPermit,
)
from .policy import (
    Clock,
    SystemClock,
    derive_restrictions,
    evaluate_qualification,
)


@dataclass(frozen=True)
class RiskItemView:
    """巡检风险项（稳定标识，重复巡检返回同一条）。"""

    id: str
    qualification_id: str
    category: RiskCategory
    reason: str
    renewal_case_id: str | None
    permit_id: str | None
    supplement_due: date | None
    grace_deadline: date | None
    created_on: date
    resolved_on: date | None


class InspectionService:
    """内存仓储的领域服务；可替换时钟，便于回放与测试。"""

    def __init__(
        self,
        clock: Clock | None = None,
        grace_days: int = 30,
    ) -> None:
        self.clock = clock or SystemClock()
        self.grace_days = grace_days
        self.qualifications: dict[str, Qualification] = {}
        self.cases: dict[str, RenewalCase] = {}
        self.permits: dict[str, TemporaryPermit] = {}
        self.suspensions: dict[str, RegulatorySuspension] = {}
        self.authorizations: dict[str, Authorization] = {}
        self.schedules: dict[str, ScheduleEntry] = {}
        self.service_records: dict[str, ServiceRecord] = {}
        self.risk_items: dict[str, RiskItemView] = {}
        self.inspection_runs: list[dict] = []
        self._seq = itertools.count(1)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------------ 登记

    def register_qualification(
        self,
        person_id: str,
        qual_type: str,
        valid_from: date,
        valid_to: date,
        qualification_id: str | None = None,
    ) -> Qualification:
        qid = qualification_id or self._new_id("Q")
        if qid in self.qualifications:
            raise ComplianceError(f"资质 {qid} 已登记")
        if valid_to < valid_from:
            raise ComplianceError("有效期截止日不能早于起始日")
        qual = Qualification(
            id=qid, person_id=person_id, qual_type=qual_type,
            valid_from=valid_from, valid_to=valid_to,
        )
        self.qualifications[qid] = qual
        return qual

    def issue_authorization(
        self,
        qualification_id: str,
        service_code: str,
        service_name: str,
        granted_from: date,
        granted_to: date | None = None,
        authorization_id: str | None = None,
    ) -> Authorization:
        qual = self._qualification(qualification_id)
        aid = authorization_id or self._new_id("A")
        auth = Authorization(
            id=aid, qualification_id=qualification_id, person_id=qual.person_id,
            service_code=service_code, service_name=service_name,
            granted_from=granted_from, granted_to=granted_to,
        )
        self.authorizations[aid] = auth
        return auth

    def add_schedule(
        self,
        authorization_id: str,
        scheduled_on: date,
        schedule_id: str | None = None,
        status: str = "pending",
    ) -> ScheduleEntry:
        auth = self._authorization(authorization_id)
        today = self.clock.today()
        # 新排班守卫：未来日期按“排班当日”投影全部限制（含已知到期日、
        # 补件冻结、监管暂停与宽限凭证边界）；历史/当日补录不拦截。
        if scheduled_on >= today:
            reason = self._scheduling_block_reason(auth, scheduled_on)
            if reason is not None:
                raise ComplianceError(
                    f"{scheduled_on.isoformat()} 不可为人员 {auth.person_id} 排班：{reason}"
                )
        entry = ScheduleEntry(
            id=schedule_id or self._new_id("S"),
            authorization_id=authorization_id, person_id=auth.person_id,
            service_code=auth.service_code, scheduled_on=scheduled_on,
            status=status,
        )
        self.schedules[entry.id] = entry
        return entry

    def add_service_record(
        self, authorization_id: str, service_date: date, service_name: str
    ) -> ServiceRecord:
        auth = self._authorization(authorization_id)
        record = ServiceRecord(
            id=self._new_id("R"), authorization_id=authorization_id,
            person_id=auth.person_id, service_date=service_date,
            service_name=service_name,
        )
        self.service_records[record.id] = record
        return record

    def service_record_basis(self, record_id: str) -> dict:
        """还原历史服务当日的授权依据；后续限制不改变历史结论。"""
        record = self.service_records.get(record_id)
        if record is None:
            raise NotFoundError(f"服务记录 {record_id} 不存在")
        auth = self._authorization(record.authorization_id)
        on = record.service_date
        in_window = auth.granted_from <= on and (
            auth.granted_to is None or on <= auth.granted_to
        )
        active = [
            r for r in auth.restrictions if r.is_active(on)
        ]
        return {
            "record_id": record.id,
            "service_date": on,
            "authorization_id": auth.id,
            "authorization_in_window": in_window,
            "active_restrictions": [r.reason for r in active],
            "practice_was_lawful": in_window and not any(
                r.kind is RestrictionKind.PRACTICE_SUSPENDED for r in active
            ),
        }

    # ------------------------------------------------------------ 续期案件

    def submit_renewal(
        self,
        qualification_id: str,
        required_documents: list[str],
        submitted_documents: list[str] | None = None,
        case_id: str | None = None,
    ) -> RenewalCase:
        """提交续期案件。材料可在提交时部分缺失（后续走待补件）。"""
        qual = self._qualification(qualification_id)
        if qual.status is QualificationStatus.ARCHIVED:
            raise ComplianceError("已归档资质不能提交续期")
        for case in self._cases_of(qualification_id):
            if case.status in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING):
                raise ComplianceError(f"已有在途续期案件 {case.id}")
        submitted = set(submitted_documents or [])
        docs = [
            RenewalDocument(
                doc_id=self._new_id("D"), name=name, present=name in submitted
            )
            for name in required_documents
        ]
        case = RenewalCase(
            id=case_id or self._new_id("C"),
            qualification_id=qualification_id,
            submitted_at=self.clock.today(),
            documents=docs,
        )
        self.cases[case.id] = case
        return case

    def request_supplement(self, case_id: str, due: date) -> RenewalCase:
        """核验发现材料不齐：案件转待补件，限期补齐，自当日冻结新排班。"""
        case = self._case(case_id)
        if case.status is not RenewalStatus.SUBMITTED:
            raise ComplianceError("仅已提交待核验的案件可以要求补件")
        if not case.missing_documents():
            raise ComplianceError("材料齐全，无需补件，应直接核验")
        if due < self.clock.today():
            raise ComplianceError("补件截止日不能早于今日")
        case.status = RenewalStatus.SUPPLEMENTING
        case.supplement_due = due
        case.supplement_requested_at = self.clock.today()
        return case

    def submit_supplement_documents(
        self, case_id: str, document_names: list[str]
    ) -> RenewalCase:
        """补证：材料到达，但仍需核验——案件回到“待核验”，限制保持冻结。"""
        case = self._case(case_id)
        if case.status is not RenewalStatus.SUPPLEMENTING:
            raise ComplianceError("仅待补件案件可以补交材料")
        received = set(document_names)
        for doc in case.documents:
            if doc.name in received:
                doc.present = True
        if not case.missing_documents():
            # 材料已齐：等待核验结论，冻结在核验通过前不解除。
            case.status = RenewalStatus.SUBMITTED
        return case

    def verify_renewal(self, case_id: str, new_valid_to: date) -> RenewalCase:
        """复核专家核验通过：写入新有效期，风险与限制在下次巡检时清结。"""
        case = self._case(case_id)
        if case.status not in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING):
            raise ComplianceError("终局案件不能再次核验")
        if case.missing_documents():
            raise ComplianceError(
                "材料不齐不能核验通过，应先要求补件："
                + "、".join(case.missing_documents())
            )
        today = self.clock.today()
        case.status = RenewalStatus.VERIFIED
        case.verified_at = today
        case.decided_valid_to = new_valid_to
        qual = self._qualification(case.qualification_id)
        qual.new_valid_to = new_valid_to
        qual.decided_at = today
        return case

    def reject_renewal(self, case_id: str, reason: str) -> RenewalCase:
        case = self._case(case_id)
        if case.status not in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING):
            raise ComplianceError("终局案件不能驳回")
        case.status = RenewalStatus.REJECTED
        case.rejected_at = self.clock.today()
        case.reject_reason = reason
        return case

    def withdraw_renewal(self, case_id: str) -> RenewalCase:
        """申请人撤回续期：案件终局，关联临时凭证立即失效。"""
        case = self._case(case_id)
        if case.status not in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING):
            raise ComplianceError("终局案件不能撤回")
        case.status = RenewalStatus.WITHDRAWN
        case.withdrawn_at = self.clock.today()
        return case

    # ------------------------------------------------------------ 临时凭证

    def issue_permit(
        self,
        qualification_id: str,
        renewal_case_id: str,
        valid_from: date,
        valid_to: date,
        permit_id: str | None = None,
        scope_note: str = "宽限期临时执业",
    ) -> TemporaryPermit:
        case = self._case(renewal_case_id)
        if case.qualification_id != qualification_id:
            raise ComplianceError("临时凭证必须关联本资质的续期案件")
        if valid_to < valid_from:
            raise ComplianceError("凭证有效期不合法")
        permit = TemporaryPermit(
            id=permit_id or self._new_id("P"),
            qualification_id=qualification_id,
            renewal_case_id=renewal_case_id,
            valid_from=valid_from, valid_to=valid_to, scope_note=scope_note,
        )
        self.permits[permit.id] = permit
        return permit

    # ------------------------------------------------------------ 监管暂停

    def suspend_by_regulator(
        self,
        qualification_id: str,
        reason: str,
        effective_from: date | None = None,
        suspension_id: str | None = None,
    ) -> RegulatorySuspension:
        self._qualification(qualification_id)
        today = self.clock.today()
        suspension = RegulatorySuspension(
            id=suspension_id or self._new_id("X"),
            qualification_id=qualification_id,
            effective_from=effective_from or today,
            reason=reason,
        )
        self.suspensions[suspension.id] = suspension
        return suspension

    def lift_suspension(
        self,
        suspension_id: str,
        effective_date: date | None = None,
    ) -> RegulatorySuspension:
        """解除监管暂停。解除日默认今日，可指定未来日期（到期自动解除）。"""
        suspension = self.suspensions.get(suspension_id)
        if suspension is None:
            raise NotFoundError(f"监管暂停 {suspension_id} 不存在")
        today = self.clock.today()
        suspension.lifted_at = today
        suspension.lift_effective_date = effective_date or today
        return suspension

    # -------------------------------------------------------------- 巡检

    def run_inspection(self, as_of: date | None = None) -> dict:
        """按（可注入的）时钟对全部资质巡检一次。

        同一日期重复执行：风险项编号、限制集合、排班取消结果全部稳定。
        """
        today = as_of or self.clock.today()
        opened = resolved = 0
        for qual in list(self.qualifications.values()):
            o, r = self._reconcile_qualification(qual, today)
            opened += o
            resolved += r
        cancelled = self._propagate_schedules(today)
        run = {
            "run_id": self._new_id("I"),
            "as_of": today,
            "risk_opened": opened,
            "risk_resolved": resolved,
            "schedules_cancelled": cancelled,
        }
        self.inspection_runs.append(run)
        return run

    def open_risk_items(self, qualification_id: str | None = None) -> list[RiskItemView]:
        items = [
            r for r in self.risk_items.values()
            if r.resolved_on is None
            and (qualification_id is None or r.qualification_id == qualification_id)
        ]
        return sorted(items, key=lambda r: (r.qualification_id, r.category.value))

    def _reconcile_qualification(
        self, qual: Qualification, today: date
    ) -> tuple[int, int]:
        cases = self._cases_of(qual.id)
        permits = [p for p in self.permits.values() if p.qualification_id == qual.id]
        suspensions = [
            s for s in self.suspensions.values() if s.qualification_id == qual.id
        ]
        draft = evaluate_qualification(
            qual, cases, permits, today, self.grace_days
        )
        open_items = [
            r for r in self.risk_items.values()
            if r.qualification_id == qual.id and r.resolved_on is None
        ]

        opened = resolved = 0
        if draft is None:
            for item in open_items:
                self.risk_items[item.id] = RiskItemView(
                    **{**item.__dict__, "resolved_on": today}
                )
                resolved += 1
        else:
            matching = next(
                (
                    i for i in open_items
                    if i.category is draft.category
                    and i.renewal_case_id == draft.renewal_case_id
                ),
                None,
            )
            if matching is None:
                for item in open_items:
                    self.risk_items[item.id] = RiskItemView(
                        **{**item.__dict__, "resolved_on": today}
                    )
                    resolved += 1
                item_id = self._new_id("RK")
                self.risk_items[item_id] = RiskItemView(
                    id=item_id, qualification_id=qual.id,
                    category=draft.category, reason=draft.reason,
                    renewal_case_id=draft.renewal_case_id,
                    permit_id=draft.permit_id,
                    supplement_due=draft.supplement_due,
                    grace_deadline=draft.grace_deadline,
                    created_on=today, resolved_on=None,
                )
                opened += 1

        self._reconcile_restrictions(
            qual, draft, suspensions, cases, permits, today
        )
        self._reconcile_qualification_status(qual, draft, cases, suspensions, today)
        return opened, resolved

    def _reconcile_restrictions(
        self,
        qual: Qualification,
        draft,
        suspensions: list[RegulatorySuspension],
        cases: list[RenewalCase],
        permits: list[TemporaryPermit],
        today: date,
    ) -> None:
        desired = derive_restrictions(
            qual, draft, suspensions, today, cases, permits, self.grace_days
        )
        desired_by_key = {
            (r.kind, r.source_type, r.source_id): r for r in desired
        }
        for auth in self._authorizations_of(qual.id):
            for existing in list(auth.restrictions):
                key = (existing.kind, existing.source_type, existing.source_id)
                wanted = desired_by_key.get(key)
                if wanted is None:
                    # 期望集合中已不存在：监管暂停按解除日收口，其余按巡检日。
                    if existing.effective_to is None:
                        existing.effective_to = today - timedelta(days=1)
                    continue
                if existing.effective_to is None and wanted.effective_to is not None:
                    # 解除日刚登记：把开放窗口收口到解除日前一天。
                    existing.effective_to = wanted.effective_to
            open_keys = {
                (r.kind, r.source_type, r.source_id)
                for r in auth.restrictions
                if r.effective_to is None
            }
            for wanted in desired:
                key = (wanted.kind, wanted.source_type, wanted.source_id)
                # 仅开放窗口参与去重；已收口的历史窗口保留用于追溯，
                # 不阻止同一来源开启新的限制窗口。
                if key not in open_keys:
                    auth.restrictions.append(wanted)

    def _reconcile_qualification_status(
        self, qual: Qualification, draft, cases, suspensions, today: date
    ) -> None:
        if qual.status is QualificationStatus.ARCHIVED:
            return
        has_open_risk = any(
            i.qualification_id == qual.id and i.resolved_on is None
            for i in self.risk_items.values()
        )
        has_active_suspension = any(s.is_active(today) for s in suspensions)
        in_flight = any(
            c.status in (RenewalStatus.SUBMITTED, RenewalStatus.SUPPLEMENTING)
            for c in cases
        )
        if has_open_risk or has_active_suspension:
            qual.status = QualificationStatus.DISPOSED
        elif in_flight:
            qual.status = QualificationStatus.PENDING_VERIFICATION
        elif qual.decided_at is not None:
            qual.status = QualificationStatus.DECIDED
        else:
            qual.status = QualificationStatus.ACTIVE

    def _propagate_schedules(self, today: date) -> int:
        """把活动限制与宽限边界传播到未来候选排班。

        按“排班当日”投影当前事实（假设续期/暂停状态不变）：

        * 失效、待补件冻结、监管暂停：取消覆盖日的未来排班；
        * 宽限：仅凭证窗口内的排班保留，凭证截止日之后的取消；
        * 已确认排班与过去排班（历史服务依据）一律保留。

        已取消的条目重复巡检不会再处理，因此结果幂等稳定。
        """
        cancelled = 0
        future_pending = [
            s for s in self.schedules.values()
            if s.status == "pending" and s.scheduled_on >= today
        ]
        for entry in future_pending:
            auth = self._authorization(entry.authorization_id)
            reason = self._scheduling_block_reason(auth, entry.scheduled_on)
            if reason is not None:
                entry.status = "cancelled"
                entry.cancel_reason = reason
                entry.cancelled_at = today
                cancelled += 1
        return cancelled

    def _scheduling_block_reason(self, auth: Authorization, on: date) -> str | None:
        """投影到 ``on`` 当日：若不可排入新排班，返回中文原因。"""
        if on < auth.granted_from or (
            auth.granted_to is not None and on > auth.granted_to
        ):
            return "授权不在有效期内"
        # 已落账的限制（补件冻结、失效停业、监管暂停），按当日生效情况判定。
        for r in auth.active_restrictions(on):
            return f"{r.reason}；限制自 {r.effective_from.isoformat()} 生效"
        qual = self._qualification(auth.qualification_id)
        if qual.status is QualificationStatus.ARCHIVED:
            return "资质已归档，不得排班"
        # 前瞻投影：今天未到期但排班日已到期/补件逾期，同样拦截；
        # 宽限（GRACE：管理宽限期或临时凭证窗口内）允许排班。
        draft = evaluate_qualification(
            qual,
            self._cases_of(qual.id),
            [p for p in self.permits.values() if p.qualification_id == qual.id],
            on,
            self.grace_days,
        )
        if draft is None or draft.category is RiskCategory.GRACE:
            return None
        return draft.reason

    def propagate_restrictions(self) -> dict:
        today = self.clock.today()
        return {"as_of": today, "schedules_cancelled": self._propagate_schedules(today)}

    # -------------------------------------------------------------- 查询

    def authorization_view(self, authorization_id: str) -> dict:
        auth = self._authorization(authorization_id)
        today = self.clock.today()
        from .policy import restriction_effective_explanation
        return {
            "authorization_id": auth.id,
            "qualification_id": auth.qualification_id,
            "person_id": auth.person_id,
            "service": {"code": auth.service_code, "name": auth.service_name},
            "granted_from": auth.granted_from,
            "granted_to": auth.granted_to,
            "can_practice_today": auth.can_practice(today),
            "can_be_scheduled_today": auth.can_be_scheduled(today),
            "restrictions": [
                {
                    "kind": r.kind.value,
                    "source_type": r.source_type,
                    "source_id": r.source_id,
                    "reason": r.reason,
                    "effective_from": r.effective_from,
                    "effective_to": r.effective_to,
                    "is_active": r.is_active(today),
                    "effect_explanation": restriction_effective_explanation(r, today),
                }
                for r in auth.restrictions
            ],
        }

    def archive_qualification(self, qualification_id: str) -> Qualification:
        qual = self._qualification(qualification_id)
        qual.status = QualificationStatus.ARCHIVED
        qual.archived_at = self.clock.today()
        return qual

    # -------------------------------------------------------------- 辅助

    def _qualification(self, qid: str) -> Qualification:
        qual = self.qualifications.get(qid)
        if qual is None:
            raise NotFoundError(f"资质 {qid} 不存在")
        return qual

    def _case(self, cid: str) -> RenewalCase:
        case = self.cases.get(cid)
        if case is None:
            raise NotFoundError(f"续期案件 {cid} 不存在")
        return case

    def _authorization(self, aid: str) -> Authorization:
        auth = self.authorizations.get(aid)
        if auth is None:
            raise NotFoundError(f"授权 {aid} 不存在")
        return auth

    def _cases_of(self, qualification_id: str) -> list[RenewalCase]:
        return [
            c for c in self.cases.values() if c.qualification_id == qualification_id
        ]

    def _authorizations_of(self, qualification_id: str) -> list[Authorization]:
        return [
            a for a in self.authorizations.values()
            if a.qualification_id == qualification_id
        ]
