"""应用服务层：命令处理、月度巡检与授权限制传播。

命令（登记/续期/补证/撤回/暂停等）在成功后立即重算相关人员的限制；
月度巡检按 ``(period)`` 幂等：重复执行得到相同风险项集合与统计。

限制生效规则见 :mod:`qualification_inspection.policy` 与各命令返回的
``effective_from``/``effective_to``。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Iterable

from .clock import Clock, SystemClock
from .models import (
    LEVEL_BLOCK,
    LEVEL_WARN,
    RESTRICTION_EXPIRING,
    RESTRICTION_GRACE,
    RESTRICTION_LAPSED,
    RESTRICTION_SUPPLEMENT_HOLD,
    RESTRICTION_SUSPENSION,
    RENEWAL_APPROVED,
    RENEWAL_PENDING,
    RENEWAL_REJECTED,
    RENEWAL_SUPPLEMENTED,
    RENEWAL_SUPPLEMENT_REQUESTED,
    RENEWAL_WITHDRAWN,
    RISK_CLEARED,
    RISK_EXPIRING_SOON,
    RISK_GRACE,
    RISK_LAPSED,
    RISK_SUSPENDED,
    RISK_SUPPLEMENT_DUE,
    RISK_TITLES,
    Authorization,
    InspectionRun,
    Person,
    Qualification,
    RenewalCase,
    Restriction,
    RiskItem,
    ServiceRecord,
    Suspension,
    TemporaryCredential,
)
from .policy import (
    RiskVerdict,
    evaluate_qualification,
)
from .repository import JsonRepository, Repository

# 风险类别 → 派生限制类型
RISK_TO_RESTRICTION = {
    RISK_EXPIRING_SOON: RESTRICTION_EXPIRING,
    RISK_GRACE: RESTRICTION_GRACE,
    RISK_LAPSED: RESTRICTION_LAPSED,
}
RESTRICTION_TITLES = {
    RESTRICTION_SUSPENSION: "监管暂停",
    RESTRICTION_SUPPLEMENT_HOLD: "补件逾期冻结",
    RESTRICTION_LAPSED: "资质失效停权",
    RESTRICTION_GRACE: "续期宽限提示",
    RESTRICTION_EXPIRING: "临期提示",
}


class DomainError(Exception):
    """业务规则冲突。"""


def _d(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class QualificationService:
    def __init__(self, repo: Repository | str | None = None, clock: Clock | None = None) -> None:
        if isinstance(repo, Repository):
            self.repo = repo
        elif isinstance(repo, str):
            self.repo = JsonRepository(repo)
        else:
            self.repo = Repository()
        self.clock = clock or SystemClock()

    @property
    def today(self) -> date:
        return self.clock.today()

    def save(self) -> None:
        if isinstance(self.repo, JsonRepository):
            self.repo.save()

    # ── 登记 ────────────────────────────────────────────────────────────
    def register_person(self, person_id: str, name: str) -> Person:
        if self.repo.persons.get(person_id):
            raise DomainError(f"人员已存在: {person_id}")
        person = Person(person_id=person_id, name=name)
        self.repo.persons.put(person)
        self.save()
        return person

    def register_qualification(
        self,
        qual_id: str,
        person_id: str,
        qual_type: str,
        qual_number: str,
        issued_on: date | str,
        expiry_on: date | str,
    ) -> Qualification:
        if self.repo.qualifications.get(qual_id):
            raise DomainError(f"资质已存在: {qual_id}")
        self.repo.persons.require(person_id)
        issued, expiry = _d(issued_on), _d(expiry_on)
        assert issued is not None and expiry is not None
        if expiry <= issued:
            raise DomainError("到期日必须晚于签发日")
        qual = Qualification(
            qual_id=qual_id, person_id=person_id, qual_type=qual_type,
            qual_number=qual_number, issued_on=issued, expiry_on=expiry,
        )
        self.repo.qualifications.put(qual)
        self._reconcile_person(person_id)
        self.save()
        return qual

    def register_authorization(
        self,
        authorization_id: str,
        person_id: str,
        qual_id: str,
        scope: str,
        granted_on: date | str | None = None,
    ) -> Authorization:
        if self.repo.authorizations.get(authorization_id):
            raise DomainError(f"授权已存在: {authorization_id}")
        qual = self.repo.qualifications.require(qual_id)
        if qual.person_id != person_id:
            raise DomainError("授权资质不属于该人员")
        auth = Authorization(
            authorization_id=authorization_id, person_id=person_id, qual_id=qual_id,
            scope=scope, granted_on=_d(granted_on) or self.today,
        )
        self.repo.authorizations.put(auth)
        self.save()
        return auth

    def revoke_authorization(self, authorization_id: str, on: date | str | None = None) -> Authorization:
        auth = self.repo.authorizations.require(authorization_id)
        auth.revoked_on = _d(on) or self.today
        self.repo.authorizations.put(auth)
        self.save()
        return auth

    # ── 续期案件 ────────────────────────────────────────────────────────
    def submit_renewal(
        self,
        case_id: str,
        qual_id: str,
        submitted_on: date | str | None = None,
    ) -> RenewalCase:
        if self.repo.renewal_cases.get(case_id):
            raise DomainError(f"续期案件号已存在: {case_id}")
        qual = self.repo.qualifications.require(qual_id)
        if any(
            c.qual_id == qual_id and c.status in RENEWAL_PENDING
            for c in self.repo.renewal_cases.all()
        ):
            raise DomainError("该资质已有在途续期案件，禁止重复提交")
        case = RenewalCase(
            case_id=case_id, qual_id=qual_id,
            submitted_on=_d(submitted_on) or self.today,
        )
        self.repo.renewal_cases.put(case)
        self._reconcile_person(qual.person_id)
        self.save()
        return case

    def request_supplement(
        self,
        case_id: str,
        items: Iterable[str],
        deadline: date | str,
        requested_on: date | str | None = None,
    ) -> RenewalCase:
        case = self._require_pending_case(case_id)
        deadline_d = _d(deadline)
        assert deadline_d is not None
        on = _d(requested_on) or self.today
        items = list(items)
        if not items:
            raise DomainError("补件要求至少包含一项材料")
        if deadline_d < on:
            raise DomainError("补件截止日不能早于要求日")
        case.supplement_rounds.append(
            {"requested_on": on.isoformat(), "deadline": deadline_d.isoformat(), "items": items}
        )
        case.status = RENEWAL_SUPPLEMENT_REQUESTED
        case.supplement_requested_on = on
        case.supplement_deadline = deadline_d
        case.supplement_items = items
        self.repo.renewal_cases.put(case)
        self._reconcile_person(self._person_of_case(case))
        self.save()
        return case

    def submit_supplement(
        self,
        case_id: str,
        doc_names: Iterable[str],
        submitted_on: date | str | None = None,
    ) -> RenewalCase:
        """补证：提交后案件回到待核验，补件冻结于提交当日解除。"""
        case = self._require_pending_case(case_id)
        if case.status != RENEWAL_SUPPLEMENT_REQUESTED:
            raise DomainError("案件当前不处于待补件状态，无需补证")
        on = _d(submitted_on) or self.today
        names = list(doc_names)
        if not names:
            raise DomainError("至少补交一项材料")
        for name in names:
            case.docs.append({"name": name, "submitted_on": on.isoformat()})
        case.status = RENEWAL_SUPPLEMENTED
        self.repo.renewal_cases.put(case)
        self._reconcile_person(self._person_of_case(case))
        self.save()
        return case

    def approve_renewal(
        self,
        case_id: str,
        new_expiry_on: date | str,
        decided_on: date | str | None = None,
    ) -> RenewalCase:
        case = self._require_pending_case(case_id)
        qual = self.repo.qualifications.require(case.qual_id)
        new_expiry = _d(new_expiry_on)
        assert new_expiry is not None
        on = _d(decided_on) or self.today
        if new_expiry <= on:
            raise DomainError("核准的新到期日必须晚于核准日")
        case.status = RENEWAL_APPROVED
        case.decided_on = on
        case.new_expiry_on = new_expiry
        # 续期核准：资质有效期顺延，风险与派生限制随之消除
        qual.expiry_on = new_expiry
        self.repo.renewal_cases.put(case)
        self.repo.qualifications.put(qual)
        self._reconcile_person(qual.person_id)
        self.save()
        return case

    def reject_renewal(
        self, case_id: str, reason: str, decided_on: date | str | None = None
    ) -> RenewalCase:
        case = self._require_pending_case(case_id)
        on = _d(decided_on) or self.today
        case.status = RENEWAL_REJECTED
        case.decided_on = on
        case.reject_reason = reason
        self.repo.renewal_cases.put(case)
        self._reconcile_person(self._person_of_case(case))
        self.save()
        return case

    def withdraw_renewal(
        self, case_id: str, on: date | str | None = None
    ) -> RenewalCase:
        """撤回续期：若资质已过期，宽限于撤回当日终结，次日起失效。"""
        case = self._require_pending_case(case_id)
        day = _d(on) or self.today
        case.status = RENEWAL_WITHDRAWN
        case.withdrawn_on = day
        self.repo.renewal_cases.put(case)
        qual = self.repo.qualifications.require(case.qual_id)
        self._reconcile_person(qual.person_id)
        self.save()
        return case

    # ── 临时凭证 ────────────────────────────────────────────────────────
    def issue_credential(
        self,
        cred_id: str,
        case_id: str,
        valid_from: date | str,
        valid_to: date | str,
        issued_on: date | str | None = None,
    ) -> TemporaryCredential:
        if self.repo.credentials.get(cred_id):
            raise DomainError(f"临时凭证号已存在: {cred_id}")
        case = self.repo.renewal_cases.require(case_id)
        if case.status not in RENEWAL_PENDING:
            raise DomainError("只能为在途续期案件签发临时凭证")
        vf, vt = _d(valid_from), _d(valid_to)
        assert vf is not None and vt is not None
        if vt < vf:
            raise DomainError("凭证失效日不能早于生效日")
        cred = TemporaryCredential(
            cred_id=cred_id, case_id=case_id, qual_id=case.qual_id,
            valid_from=vf, valid_to=vt, issued_on=_d(issued_on) or self.today,
        )
        self.repo.credentials.put(cred)
        self._reconcile_person(self._person_of_case(case))
        self.save()
        return cred

    def void_credential(
        self, cred_id: str, reason: str, on: date | str | None = None
    ) -> TemporaryCredential:
        cred = self.repo.credentials.require(cred_id)
        if cred.voided_on is not None:
            raise DomainError("凭证已作废")
        cred.voided_on = _d(on) or self.today
        cred.void_reason = reason
        self.repo.credentials.put(cred)
        case = self.repo.renewal_cases.require(cred.case_id)
        self._reconcile_person(self._person_of_case(case))
        self.save()
        return cred

    # ── 监管暂停 ────────────────────────────────────────────────────────
    def suspend_person(
        self,
        order_no: str,
        person_id: str,
        reason: str,
        start_on: date | str,
        end_on: date | str | None = None,
        scope: str | None = None,
    ) -> Restriction:
        if self.repo.suspensions.get(order_no):
            raise DomainError(f"暂停文书号已存在: {order_no}")
        self.repo.persons.require(person_id)
        start, end = _d(start_on), _d(end_on)
        assert start is not None
        if end is not None and end < start:
            raise DomainError("暂停截止日不能早于起始日")
        suspension = Suspension(
            order_no=order_no, person_id=person_id, reason=reason,
            start_on=start, end_on=end, scope=scope,
        )
        self.repo.suspensions.put(suspension)
        self._reconcile_person(person_id)
        self.save()
        restriction = self.repo.restrictions.get(f"SUSP-{order_no}")
        assert restriction is not None
        return restriction

    def lift_suspension(
        self, order_no: str, lifted_on: date | str | None = None
    ) -> Suspension:
        suspension = self.repo.suspensions.require(order_no)
        day = _d(lifted_on) or self.today
        suspension.lifted_on = day
        self.repo.suspensions.put(suspension)
        self._reconcile_person(suspension.person_id)
        self.save()
        return suspension

    # ── 历史服务依据 ────────────────────────────────────────────────────
    def record_service(
        self,
        record_id: str,
        person_id: str,
        authorization_id: str,
        service_on: date | str,
        detail: str = "",
    ) -> ServiceRecord:
        """登记已发生服务并固化资质快照；事后停权不改变该依据。"""
        if self.repo.service_records.get(record_id):
            raise DomainError(f"服务记录已存在: {record_id}")
        auth = self.repo.authorizations.require(authorization_id)
        if auth.person_id != person_id:
            raise DomainError("授权不属于该人员")
        qual = self.repo.qualifications.require(auth.qual_id)
        day = _d(service_on)
        assert day is not None
        record = ServiceRecord(
            record_id=record_id, person_id=person_id, authorization_id=authorization_id,
            scope=auth.scope, service_on=day, qual_type=qual.qual_type,
            qual_number=qual.qual_number, qual_expiry_on=qual.expiry_on, detail=detail,
        )
        self.repo.service_records.put(record)
        self.save()
        return record

    # ── 月度巡检（幂等）────────────────────────────────────────────────
    def current_period(self) -> str:
        return self.today.strftime("%Y-%m")

    def run_monthly_inspection(self, period: str | None = None) -> dict[str, Any]:
        """执行（或重复执行）某月巡检。

        同一 ``period`` 重复执行：风险项按资质幂等更新，统计反映本次执行，
        并记录/覆盖一条 :class:`InspectionRun`。
        """
        period = period or self.current_period()
        created = updated = cleared = stable = 0
        risk_total = 0

        for qual in self.repo.qualifications.all():
            cases = self.repo.renewal_cases.filter(qual_id=qual.qual_id)
            creds = self.repo.credentials.filter(qual_id=qual.qual_id)
            verdict = evaluate_qualification(qual, cases, creds, self.today)

            open_risks = [
                r for r in self.repo.risks.filter(qual_id=qual.qual_id)
                if r.resolved_on is None
            ]
            open_risk = max(open_risks, key=lambda r: r.first_found_on, default=None)

            if verdict is None:
                if open_risk is not None:
                    self._clear_risk(open_risk)
                    cleared += 1
                continue

            risk_total += 1
            if open_risk is None:
                self.repo.risks.put(self._new_risk(period, qual, verdict))
                created += 1
            elif open_risk.category != verdict.category or open_risk.level != verdict.level:
                open_risk.transitions.append({
                    "from": open_risk.category,
                    "from_level": open_risk.level,
                    "to": verdict.category,
                    "to_level": verdict.level,
                    "on": self.today.isoformat(),
                })
                open_risk.category = verdict.category
                open_risk.level = verdict.level
                open_risk.title = RISK_TITLES[verdict.category]
                open_risk.detail = verdict.detail
                open_risk.effective_from = verdict.effective_from
                open_risk.hardens_on = verdict.hardens_on
                open_risk.last_seen_on = self.today
                self.repo.risks.put(open_risk)
                updated += 1
            else:
                # 稳定状态：仅刷新最近发现日与说明，类别、级别、生效日不变
                open_risk.last_seen_on = self.today
                open_risk.detail = verdict.detail
                self.repo.risks.put(open_risk)
                stable += 1

        # 人员级风险：监管暂停（风险项跨月稳定，重复巡检只刷新发现日）
        active_orders = set()
        for suspension in self.repo.suspensions.all():
            if suspension.active_on(self.today):
                active_orders.add(suspension.order_no)
                risk_total += 1
                rid = f"R-SUSP-{suspension.order_no}"
                existing = self.repo.risks.get(rid)
                if existing is None:
                    self.repo.risks.put(RiskItem(
                        risk_id=rid, period=period, qual_id="",
                        person_id=suspension.person_id, category=RISK_SUSPENDED,
                        level=LEVEL_BLOCK, title=RISK_TITLES[RISK_SUSPENDED],
                        detail=f"监管暂停文书 {suspension.order_no}：{suspension.reason}",
                        first_found_on=self.today, last_seen_on=self.today,
                        effective_from=suspension.start_on,
                    ))
                    created += 1
                else:
                    existing.period = period
                    existing.last_seen_on = self.today
                    self.repo.risks.put(existing)
                    stable += 1

        # 已解除/到期的暂停：关闭其遗留的暂停风险项
        for risk in self.repo.risks.all():
            if risk.category != RISK_SUSPENDED or risk.resolved_on is not None:
                continue
            order_no = risk.risk_id.removeprefix("R-SUSP-")
            if order_no not in active_orders:
                risk.transitions.append({
                    "from": RISK_SUSPENDED, "to": RISK_CLEARED,
                    "on": self.today.isoformat(),
                })
                risk.category = RISK_CLEARED
                risk.title = RISK_TITLES[RISK_CLEARED]
                risk.detail = "监管暂停已解除或到期"
                risk.resolved_on = self.today
                self.repo.risks.put(risk)
                cleared += 1

        for person in self.repo.persons.all():
            self._reconcile_person(person.person_id)

        run = InspectionRun(
            period=period, ran_on=self.today, risk_total=risk_total,
            created=created, updated=updated, cleared=cleared, stable=stable,
        )
        self.repo.runs.put(run)
        self.save()
        return run.to_dict()

    def list_risks(
        self, person_id: str | None = None, include_cleared: bool = False
    ) -> list[dict[str, Any]]:
        items = self.repo.risks.all()
        if person_id is not None:
            items = [r for r in items if r.person_id == person_id]
        if not include_cleared:
            items = [r for r in items if r.resolved_on is None]
        items.sort(key=lambda r: (r.first_found_on, r.risk_id))
        return [r.to_dict() for r in items]

    def list_restrictions(
        self,
        person_id: str | None = None,
        active_on: date | str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        day = _d(active_on)
        items = self.repo.restrictions.all()
        if person_id is not None:
            items = [r for r in items if r.person_id == person_id]
        if day is not None:
            items = [r for r in items if r.applies_to(day)]
        elif not include_inactive:
            items = [r for r in items if r.active]
        items.sort(key=lambda r: (r.effective_from, r.restriction_id))
        return [self._restriction_view(r) for r in items]

    # ── 排班影响 ────────────────────────────────────────────────────────
    def evaluate_schedule(self, person_id: str, day: date | str) -> dict[str, Any]:
        """评估某人在指定日期的排班可行性与全部生效/将生效的限制。

        返回每项限制的 ``effective_from``（含）与 ``effective_to``（含），
        接口调用方可据此向排班系统解释何时生效、何时解除。
        """
        self.repo.persons.require(person_id)
        day_d = _d(day)
        assert day_d is not None
        restrictions = [
            r for r in self.repo.restrictions.all()
            if r.person_id == person_id and r.applies_to(day_d)
        ]
        auth_views = []
        person_blocked = False
        for auth in self.repo.authorizations.filter(person_id=person_id):
            if auth.revoked_on is not None:
                continue
            hits = [r for r in restrictions if self._restriction_hits_auth(r, auth)]
            blocked = any(r.level == LEVEL_BLOCK for r in hits)
            person_blocked = person_blocked or blocked
            auth_views.append({
                "authorization_id": auth.authorization_id,
                "scope": auth.scope,
                "qual_id": auth.qual_id,
                "allowed": not blocked,
                "restrictions": [self._restriction_view(r) for r in hits],
            })
        return {
            "person_id": person_id,
            "day": day_d.isoformat(),
            "allowed": not person_blocked,
            "authorizations": auth_views,
        }

    def future_schedule_impact(
        self,
        person_id: str,
        from_on: date | str | None = None,
        through: date | str | None = None,
    ) -> dict[str, Any]:
        """列出限制对未来排班的传播窗口；历史服务记录不参与且不受影响。"""
        start = _d(from_on) or self.today
        end = _d(through) or (start + timedelta(days=180))
        self.repo.persons.require(person_id)
        active = [
            r for r in self.repo.restrictions.all()
            if r.person_id == person_id
            and r.active
            and r.effective_from <= end
            and (r.effective_to is None or r.effective_to >= start)
        ]
        windows = []
        for r in sorted(active, key=lambda x: x.effective_from):
            if r.effective_from > end:
                continue
            auth_ids = [
                a.authorization_id for a in self.repo.authorizations.filter(person_id=person_id)
                if a.revoked_on is None and self._restriction_hits_auth(r, a)
            ]
            windows.append({
                **self._restriction_view(r),
                "affects_authorizations": auth_ids,
                "status": "ACTIVE" if r.effective_from <= start else "SCHEDULED",
            })
        return {
            "person_id": person_id,
            "from_on": start.isoformat(),
            "through": end.isoformat(),
            "windows": windows,
            "note": "限制仅传播到未来排班；已发生服务的历史依据见服务记录，不受限制影响",
        }

    # ── 限制重算（派生）─────────────────────────────────────────────────
    def _reconcile_person(self, person_id: str) -> None:
        """根据当前时钟日重建某人的全部限制。

        派生于资质风险的限制删除后按稳定 ID 重放；监管暂停限制与暂停
        文书保持一一对应。关闭旧限制时尽量回填准确的终止日，保留历史。
        """
        today = self.today
        desired: dict[str, Restriction] = {}

        # 1) 资质风险派生
        for qual in self.repo.qualifications.filter(person_id=person_id):
            cases = self.repo.renewal_cases.filter(qual_id=qual.qual_id)
            creds = self.repo.credentials.filter(qual_id=qual.qual_id)
            verdict = evaluate_qualification(qual, cases, creds, today)
            if verdict is None:
                continue
            if verdict.category == RISK_SUPPLEMENT_DUE:
                if verdict.level == LEVEL_BLOCK:
                    self._upsert_restriction(desired, Restriction(
                        restriction_id=f"DR-{person_id}-{qual.qual_id}-SUPPHOLD",
                        person_id=person_id, restriction_type=RESTRICTION_SUPPLEMENT_HOLD,
                        level=LEVEL_BLOCK, title=RESTRICTION_TITLES[RESTRICTION_SUPPLEMENT_HOLD],
                        effective_from=verdict.effective_from, source="巡检",
                        reason=verdict.detail, qual_id=qual.qual_id,
                    ))
                else:
                    # 待补件未逾期：复用宽限提示通道，携带硬化日
                    self._upsert_restriction(desired, Restriction(
                        restriction_id=f"DR-{person_id}-{qual.qual_id}-GRACE",
                        person_id=person_id, restriction_type=RESTRICTION_GRACE,
                        level=LEVEL_WARN, title="待补件提示",
                        effective_from=verdict.effective_from, source="巡检",
                        reason=verdict.detail, qual_id=qual.qual_id,
                        effective_to=(verdict.hardens_on - timedelta(days=1)
                                      if verdict.hardens_on else None),
                    ))
                continue
            rtype = RISK_TO_RESTRICTION[verdict.category]
            if verdict.category == RISK_EXPIRING_SOON:
                eff_to = qual.expiry_on
            elif verdict.category == RISK_GRACE:
                # 计划窗口到宽限上限日；提前核准/撤回时由关闭逻辑回填真实终止日
                eff_to = (verdict.hardens_on - timedelta(days=1)
                          if verdict.hardens_on else None)
            else:
                # LAPSED 的解除日尚不确定；解除时由关闭逻辑回填
                eff_to = None
            self._upsert_restriction(desired, Restriction(
                restriction_id=f"DR-{person_id}-{qual.qual_id}-{rtype}",
                person_id=person_id, restriction_type=rtype, level=verdict.level,
                title=RESTRICTION_TITLES[rtype], effective_from=verdict.effective_from,
                source="巡检", reason=verdict.detail, qual_id=qual.qual_id,
                effective_to=eff_to,
            ))

        # 2) 监管暂停派生：未解除的暂停一律建限制行（未来起始的暂停为
        #    SCHEDULED，由 effective_from 控制何时生效）；已解除的交由关闭逻辑
        for suspension in self.repo.suspensions.filter(person_id=person_id):
            rid = f"SUSP-{suspension.order_no}"
            if suspension.lifted_on is None:
                self._upsert_restriction(desired, Restriction(
                    restriction_id=rid, person_id=person_id,
                    restriction_type=RESTRICTION_SUSPENSION, level=LEVEL_BLOCK,
                    title=RESTRICTION_TITLES[RESTRICTION_SUSPENSION],
                    effective_from=suspension.start_on, source=f"监管文书 {suspension.order_no}",
                    reason=suspension.reason, scope=suspension.scope,
                    effective_to=suspension.end_on,
                ))

        # 3) 关闭不再期望的活动限制（保留历史行）
        desired_ids = set(desired)
        block_boundaries = {
            r.qual_id: r.effective_from for r in desired.values() if r.level == LEVEL_BLOCK
        }
        for existing in self.repo.restrictions.filter(person_id=person_id):
            if existing.restriction_id in desired_ids:
                continue
            if not existing.active:
                continue
            if existing.restriction_id.startswith("SUSP-"):
                order_no = existing.restriction_id.removeprefix("SUSP-")
                suspension = self.repo.suspensions.get(order_no)
                close_on = suspension.lifted_on if suspension and suspension.lifted_on else today
            elif existing.restriction_type == RESTRICTION_SUPPLEMENT_HOLD:
                # 补证提交当日即解除：终止日回填为最近一次补证日的前一日
                doc_days = [
                    date.fromisoformat(d["submitted_on"])
                    for c in self.repo.renewal_cases.filter(qual_id=existing.qual_id)
                    for d in c.docs
                ]
                close_on = (max(doc_days) - timedelta(days=1)) if doc_days else today
            else:
                boundary = block_boundaries.get(existing.qual_id)
                close_on = boundary - timedelta(days=1) if boundary else today
                if close_on < existing.effective_from:
                    close_on = today
            existing.active = False
            existing.effective_to = close_on
            self.repo.restrictions.put(existing)

        # 4) 写入期望限制
        for rid, restriction in desired.items():
            existing = self.repo.restrictions.get(rid)
            if existing is None:
                self.repo.restrictions.put(restriction)
            else:
                existing.level = restriction.level
                existing.title = restriction.title
                existing.reason = restriction.reason
                existing.effective_from = restriction.effective_from
                existing.effective_to = restriction.effective_to
                existing.scope = restriction.scope
                existing.active = True
                self.repo.restrictions.put(existing)

    @staticmethod
    def _upsert_restriction(
        desired: dict[str, Restriction], restriction: Restriction
    ) -> None:
        desired[restriction.restriction_id] = restriction

    @staticmethod
    def _restriction_hits_auth(restriction: Restriction, auth: Authorization) -> bool:
        if restriction.qual_id is not None and restriction.qual_id != auth.qual_id:
            return False
        if restriction.scope is not None and restriction.scope != auth.scope:
            return False
        return True

    @staticmethod
    def _restriction_view(r: Restriction) -> dict[str, Any]:
        return {
            "restriction_id": r.restriction_id,
            "type": r.restriction_type,
            "level": r.level,
            "title": r.title,
            "reason": r.reason,
            "source": r.source,
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
            "qual_id": r.qual_id,
            "scope": r.scope,
        }

    # ── 风险项辅助 ──────────────────────────────────────────────────────
    def _new_risk(
        self, period: str, qual: Qualification, verdict: RiskVerdict
    ) -> RiskItem:
        return RiskItem(
            risk_id=_uid(f"R-{period}-{qual.qual_id}"),
            period=period, qual_id=qual.qual_id, person_id=qual.person_id,
            category=verdict.category, level=verdict.level,
            title=RISK_TITLES[verdict.category], detail=verdict.detail,
            first_found_on=self.today, last_seen_on=self.today,
            effective_from=verdict.effective_from, hardens_on=verdict.hardens_on,
        )

    def _clear_risk(self, risk: RiskItem) -> None:
        risk.transitions.append({
            "from": risk.category, "to": RISK_CLEARED, "on": self.today.isoformat(),
        })
        risk.category = RISK_CLEARED
        risk.level = LEVEL_WARN
        risk.title = RISK_TITLES[RISK_CLEARED]
        risk.detail = "风险已消除（续期核准或有效期延长）"
        risk.resolved_on = self.today
        self.repo.risks.put(risk)

    # ── 通用辅助 ────────────────────────────────────────────────────────
    def _require_pending_case(self, case_id: str) -> RenewalCase:
        case = self.repo.renewal_cases.require(case_id)
        if case.status not in RENEWAL_PENDING:
            raise DomainError(f"案件 {case_id} 已终局（{case.status}），不可再变更")
        return case

    def _person_of_case(self, case: RenewalCase) -> str:
        return self.repo.qualifications.require(case.qual_id).person_id
