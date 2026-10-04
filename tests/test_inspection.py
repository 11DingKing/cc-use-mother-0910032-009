"""资质到期巡检后端的领域回归测试。

覆盖：待补件/宽限/失效判定、重复巡检幂等、补证与撤回、监管暂停的
稳定状态、授权限制对未来排班的传播与历史服务依据保留。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection import (
    ComplianceError,
    FixedClock,
    InspectionService,
    NotFoundError,
    QualificationStatus,
    RenewalStatus,
    RestrictionKind,
    RiskCategory,
)

D = date.fromisoformat
TODAY = D("2026-10-04")


def make_service(today: date = TODAY) -> InspectionService:
    return InspectionService(FixedClock(today))


class PolicyEvaluationTest(unittest.TestCase):
    def _person(self, svc: InspectionService, valid_to: date):
        qual = svc.register_qualification("P1", "执业证", D("2025-01-01"), valid_to, "Q1")
        auth = svc.issue_authorization("Q1", "S001", "普通门诊", D("2025-01-01"), None, "A1")
        return qual, auth

    def test_no_risk_before_expiry(self) -> None:
        svc = make_service(D("2026-09-30"))
        self._person(svc, D("2026-10-04"))
        run = svc.run_inspection()
        self.assertEqual(run["risk_opened"], 0)
        self.assertEqual(svc.open_risk_items(), [])
        self.assertEqual(svc.qualifications["Q1"].status, QualificationStatus.ACTIVE)

    def test_expired_without_renewal(self) -> None:
        svc = make_service(D("2026-10-05"))
        _, auth = self._person(svc, D("2026-10-04"))
        svc.run_inspection()
        items = svc.open_risk_items()
        self.assertEqual(len(items), 1)
        self.assertIs(items[0].category, RiskCategory.EXPIRED)
        # 有效期截止次日才停业，生效日明确。
        view = svc.authorization_view("A1")
        self.assertFalse(view["can_practice_today"])
        susp = [r for r in view["restrictions"] if r["kind"] == "practice_suspended"][0]
        self.assertEqual(susp["effective_from"], D("2026-10-05"))
        self.assertTrue(susp["is_active"])

    def test_grace_with_pending_renewal_and_permit(self) -> None:
        svc = make_service(D("2026-10-05"))
        _, auth = self._person(svc, D("2026-10-04"))
        case = svc.submit_renewal("Q1", ["执业证原件", "体检表"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-11-04"), "P1")
        svc.run_inspection()
        items = svc.open_risk_items()
        self.assertEqual(len(items), 1)
        self.assertIs(items[0].category, RiskCategory.GRACE)
        view = svc.authorization_view("A1")
        # 宽限期内继续执业，且无新增限制。
        self.assertTrue(view["can_practice_today"])
        self.assertEqual(view["restrictions"], [])

    def test_admin_grace_when_renewal_submitted_before_expiry_without_permit(self) -> None:
        # 到期前一天已提交续期、尚无临时凭证：到期次日进入管理宽限，不误伤。
        svc = make_service(D("2026-10-03"))
        _, auth = self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.clock.advance(2)  # 2026-10-05，已过期 1 天
        svc.run_inspection()
        items = svc.open_risk_items()
        self.assertIs(items[0].category, RiskCategory.GRACE)
        self.assertEqual(items[0].grace_deadline, D("2026-11-03"))
        self.assertTrue(auth.can_practice(D("2026-10-05")))
        # 管理宽限期满（30 天）后失效。
        svc.clock.advance(29)  # 2026-11-03 仍在宽限内
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.GRACE)
        svc.clock.advance(1)  # 2026-11-04 超出管理宽限
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)

    def test_late_renewal_after_expiry_does_not_get_admin_grace(self) -> None:
        # 到期后才补提交续期：不能套取管理宽限，无凭证即失效。
        svc = make_service(D("2026-10-05"))
        self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)

    def test_permit_extends_beyond_admin_grace_window(self) -> None:
        svc = make_service(D("2026-10-03"))
        self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-12-31"), "P1")
        svc.clock.advance(60)  # 已超管理宽限，但凭证仍覆盖
        svc.run_inspection()
        items = svc.open_risk_items()
        self.assertIs(items[0].category, RiskCategory.GRACE)
        self.assertEqual(items[0].grace_deadline, D("2026-12-31"))
        self.assertEqual(items[0].permit_id, "P1")

    def test_expired_after_permit_window_ends(self) -> None:
        svc = make_service(D("2026-11-05"))
        self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-11-04"), "P1")
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)

    def test_supplement_required_freezes_scheduling_only(self) -> None:
        svc = make_service(D("2026-09-20"))  # 尚未到期
        _, auth = self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["执业证原件", "体检表"], ["执业证原件"], "C1")
        svc.request_supplement("C1", D("2026-09-30"))
        svc.run_inspection()
        items = svc.open_risk_items()
        self.assertIs(items[0].category, RiskCategory.MISSING_DOCS)
        # 待补件：执业可继续，但不能排入新班。
        self.assertTrue(auth.can_practice(D("2026-09-20")))
        self.assertFalse(auth.can_be_scheduled(D("2026-09-20")))

    def test_supplement_overdue_after_expiry_becomes_expired(self) -> None:
        svc = make_service(D("2026-09-28"))
        self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["体检表"], [], "C1")
        svc.request_supplement("C1", D("2026-10-01"))
        svc.clock.advance(8)  # 2026-10-06：资质已到期且补件已逾期
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)

    def test_supplement_within_due_after_expiry_stays_missing_docs(self) -> None:
        svc = make_service(D("2026-10-05"))
        self._person(svc, D("2026-10-04"))
        svc.submit_renewal("Q1", ["体检表"], [], "C1")
        svc.request_supplement("C1", D("2026-10-15"))
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.MISSING_DOCS)


class IdempotentInspectionTest(unittest.TestCase):
    def test_repeated_inspection_is_stable(self) -> None:
        svc = make_service(D("2026-10-05"))
        svc.register_qualification("P1", "主诊资格", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "主诊诊疗", D("2025-01-01"), None, "A1")
        svc.run_inspection()
        first = svc.open_risk_items()
        auth_before = svc.authorization_view("A1")
        # 同日再巡检：无新增/解决，限制不重复，运行记录为 0/0。
        second_run = svc.run_inspection()
        self.assertEqual((second_run["risk_opened"], second_run["risk_resolved"]), (0, 0))
        self.assertEqual([i.id for i in svc.open_risk_items()], [i.id for i in first])
        auth_after = svc.authorization_view("A1")
        self.assertEqual(len(auth_after["restrictions"]), len(auth_before["restrictions"]))

    def test_risk_transitions_resolve_previous_item(self) -> None:
        svc = make_service(D("2026-10-05"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.run_inspection()
        expired = svc.open_risk_items()[0]
        # 次日补交在途续期并发放临时凭证，风险从失效转为宽限。
        svc.clock.advance(1)
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-06"), D("2026-11-05"), "P1")
        svc.run_inspection()
        self.assertIsNotNone(svc.risk_items[expired.id].resolved_on)
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.GRACE)
        # 失效停业限制已收口（至失效解除前一日）。
        auth = svc.authorizations["A1"]
        self.assertTrue(all(r.effective_to is not None for r in auth.restrictions))
        self.assertTrue(auth.can_practice(D("2026-10-06")))


class RenewalLifecycleTest(unittest.TestCase):
    def _setup(self, svc: InspectionService):
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")

    def test_supplement_then_verify_clears_risk(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件", "体检表"], ["执业证原件"], "C1")
        svc.request_supplement("C1", D("2026-09-30"))
        svc.run_inspection()
        self.assertEqual(len(svc.open_risk_items()), 1)
        # 补证：材料到齐，案件回到待核验，冻结仍在。
        svc.submit_supplement_documents("C1", ["体检表"])
        self.assertIs(svc.cases["C1"].status, RenewalStatus.SUBMITTED)
        self.assertFalse(svc.authorizations["A1"].can_be_scheduled(D("2026-09-21")))
        # 核验通过：写入新有效期，巡检清结风险与限制。
        svc.verify_renewal("C1", D("2028-10-04"))
        svc.run_inspection()
        self.assertEqual(svc.open_risk_items(), [])
        self.assertTrue(svc.authorizations["A1"].can_be_scheduled(D("2026-09-21")))
        self.assertEqual(svc.qualifications["Q1"].status, QualificationStatus.DECIDED)

    def test_verify_rejects_when_documents_missing(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["体检表"], [], "C1")
        with self.assertRaises(ComplianceError):
            svc.verify_renewal("C1", D("2028-10-04"))

    def test_withdraw_renewal_invalidates_permit_and_grace(self) -> None:
        svc = make_service(D("2026-10-05"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-11-04"), "P1")
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.GRACE)
        # 撤回续期：凭证立即失去依据，转失效。
        svc.withdraw_renewal("C1")
        self.assertIs(svc.cases["C1"].status, RenewalStatus.WITHDRAWN)
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)
        self.assertFalse(svc.authorizations["A1"].can_practice(D("2026-10-05")))

    def test_reject_renewal_leads_to_expired(self) -> None:
        svc = make_service(D("2026-10-05"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件"], [], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-11-04"), "P1")
        svc.reject_renewal("C1", "材料造假")
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)

    def test_terminal_case_cannot_change_again(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.withdraw_renewal("C1")
        with self.assertRaises(ComplianceError):
            svc.withdraw_renewal("C1")
        with self.assertRaises(ComplianceError):
            svc.verify_renewal("C1", D("2028-10-04"))

    def test_duplicate_in_flight_renewal_rejected(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        with self.assertRaises(ComplianceError):
            svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C2")


class RegulatorySuspensionTest(unittest.TestCase):
    def test_suspension_takes_effect_immediately(self) -> None:
        svc = make_service(D("2026-09-20"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2028-01-01"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.suspend_by_regulator("Q1", "投诉立案调查", D("2026-09-20"), "X1")
        svc.run_inspection()
        view = svc.authorization_view("A1")
        self.assertFalse(view["can_practice_today"])
        kinds = {r["kind"] for r in view["restrictions"]}
        self.assertIn("practice_suspended", kinds)
        # 暂停不是到期风险，不产生风险项。
        self.assertEqual(svc.open_risk_items(), [])
        self.assertEqual(svc.qualifications["Q1"].status, QualificationStatus.DISPOSED)

    def test_lift_suspension_restores_practice(self) -> None:
        svc = make_service(D("2026-09-20"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2028-01-01"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.suspend_by_regulator("Q1", "调查", D("2026-09-10"), "X1")
        svc.run_inspection()
        svc.clock.advance(10)  # 2026-09-30 解除
        svc.lift_suspension("X1", D("2026-09-30"))
        svc.run_inspection()
        auth = svc.authorizations["A1"]
        self.assertTrue(auth.can_practice(D("2026-09-30")))
        # 历史窗口仍可还原：暂停期内停业、解除后恢复。
        self.assertFalse(auth.can_practice(D("2026-09-20")))
        r = auth.restrictions[0]
        self.assertEqual(r.effective_from, D("2026-09-10"))
        self.assertEqual(r.effective_to, D("2026-09-29"))


class SchedulePropagationTest(unittest.TestCase):
    def _setup(self, svc: InspectionService) -> None:
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")

    def test_expiry_cancels_future_but_keeps_past_schedules(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        # 到期前一天的候选排班；新增远期排班被前瞻拦截。
        before = svc.add_schedule("A1", D("2026-10-03"), "S1")
        with self.assertRaises(ComplianceError):
            svc.add_schedule("A1", D("2026-10-10"), "S2")
        svc.clock.advance(20)  # 2026-10-10：已失效
        svc.run_inspection()
        # 到期前的排班已成为过去（历史服务依据），巡检保留。
        self.assertEqual(before.status, "pending")

    def test_inspection_cancels_existing_future_schedules(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        svc.add_schedule("A1", D("2026-10-03"), "S1")
        # 直接写入一条“限制登记前”就已存在的远期候选排班。
        from qualification_inspection.models import ScheduleEntry
        far_entry = ScheduleEntry(
            id="S2", authorization_id="A1", person_id="P1",
            service_code="S001", scheduled_on=D("2026-10-20"),
        )
        svc.schedules["S2"] = far_entry
        svc.clock.advance(20)
        run = svc.run_inspection()
        self.assertEqual(run["schedules_cancelled"], 1)
        self.assertEqual(svc.schedules["S1"].status, "pending")
        self.assertEqual(svc.schedules["S2"].status, "cancelled")
        self.assertIn("失效", svc.schedules["S2"].cancel_reason)
        # 重复巡检不重复取消。
        again = svc.run_inspection()
        self.assertEqual(again["schedules_cancelled"], 0)

    def test_grace_permit_allows_schedules_within_window_only(self) -> None:
        svc = make_service(D("2026-10-05"))
        self._setup(svc)
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-11-04"), "P1")
        svc.run_inspection()
        within = svc.add_schedule("A1", D("2026-10-20"), "S1")
        self.assertEqual(within.status, "pending")
        with self.assertRaises(ComplianceError):
            svc.add_schedule("A1", D("2026-11-10"), "S2")

    def test_supplement_freeze_cancels_future_schedules(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        from qualification_inspection.models import ScheduleEntry
        svc.schedules["S1"] = ScheduleEntry(
            id="S1", authorization_id="A1", person_id="P1",
            service_code="S001", scheduled_on=D("2026-09-25"),
        )
        svc.submit_renewal("Q1", ["体检表"], [], "C1")
        svc.request_supplement("C1", D("2026-09-30"))
        run = svc.run_inspection()
        self.assertEqual(run["schedules_cancelled"], 1)
        self.assertEqual(svc.schedules["S1"].status, "cancelled")

    def test_suspension_cancels_future_schedules_and_lift_allows_new(self) -> None:
        svc = make_service(D("2026-09-20"))
        self._setup(svc)
        from qualification_inspection.models import ScheduleEntry
        svc.schedules["S1"] = ScheduleEntry(
            id="S1", authorization_id="A1", person_id="P1",
            service_code="S001", scheduled_on=D("2026-09-25"),
        )
        svc.suspend_by_regulator("Q1", "调查", D("2026-09-21"), "X1")
        svc.run_inspection()
        self.assertEqual(svc.schedules["S1"].status, "cancelled")
        with self.assertRaises(ComplianceError):
            svc.add_schedule("A1", D("2026-09-26"))
        svc.lift_suspension("X1", D("2026-09-24"))
        svc.run_inspection()
        # 解除日之后可以重新排班。
        entry = svc.add_schedule("A1", D("2026-09-25"))
        self.assertEqual(entry.status, "pending")


class HistoricalBasisTest(unittest.TestCase):
    def test_history_service_basis_preserved_after_later_restriction(self) -> None:
        svc = make_service(D("2026-09-20"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        record = svc.add_service_record("A1", D("2026-09-15"), "普通门诊")
        # 之后资质失效、监管暂停，都不应改写历史结论。
        svc.clock.advance(20)
        svc.suspend_by_regulator("Q1", "调查", D("2026-10-05"), "X1")
        svc.run_inspection()
        basis = svc.service_record_basis(record.id)
        self.assertTrue(basis["practice_was_lawful"])
        self.assertEqual(basis["active_restrictions"], [])
        # 当天实时判定则已停业。
        self.assertFalse(svc.authorization_view("A1")["can_practice_today"])

    def test_service_during_admin_grace_stays_lawful_after_later_expiry(self) -> None:
        # 到期前提交续期；宽限期第 20 天提供的服务，
        # 在后来宽限耗尽、资质失效后仍应被认定为合法。
        svc = make_service(D("2026-10-03"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        record = svc.add_service_record("A1", D("2026-10-24"), "普通门诊")
        svc.clock.advance(40)  # 2026-11-12：管理宽限早已耗尽
        svc.run_inspection()
        self.assertIs(svc.open_risk_items()[0].category, RiskCategory.EXPIRED)
        # 失效停业自宽限结束次日（2026-11-04）生效，不回溯到到期次日。
        view = svc.authorization_view("A1")
        susp = [r for r in view["restrictions"] if r["kind"] == "practice_suspended"][0]
        self.assertEqual(susp["effective_from"], D("2026-11-04"))
        self.assertFalse(view["can_practice_today"])
        basis = svc.service_record_basis(record.id)
        self.assertTrue(basis["practice_was_lawful"])

    def test_withdraw_during_grace_suspends_from_withdrawal_day(self) -> None:
        svc = make_service(D("2026-10-03"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.submit_renewal("Q1", ["执业证原件"], ["执业证原件"], "C1")
        svc.issue_permit("Q1", "C1", D("2026-10-05"), D("2026-12-31"), "P1")
        svc.clock.advance(10)  # 2026-10-13 撤回
        svc.withdraw_renewal("C1")
        svc.run_inspection()
        view = svc.authorization_view("A1")
        susp = [r for r in view["restrictions"] if r["kind"] == "practice_suspended"][0]
        # 撤回当日即停业，而不是等到凭证名义截止日，也不早于到期次日。
        self.assertEqual(susp["effective_from"], D("2026-10-13"))
        self.assertFalse(view["can_practice_today"])


class RestrictionExplanationTest(unittest.TestCase):
    def test_explanation_says_when_effective(self) -> None:
        svc = make_service(D("2026-10-05"))
        svc.register_qualification("P1", "执业证", D("2025-01-01"), D("2026-10-04"), "Q1")
        svc.issue_authorization("Q1", "S001", "门诊", D("2025-01-01"), None, "A1")
        svc.run_inspection()
        view = svc.authorization_view("A1")
        text = view["restrictions"][0]["effect_explanation"]
        self.assertIn("生效日 2026-10-05", text)
        self.assertIn("当前生效中", text)
        self.assertIn("历史服务记录保留", text)

    def test_missing_entity_raises_not_found(self) -> None:
        svc = make_service()
        with self.assertRaises(NotFoundError):
            svc.authorization_view("NOPE")


if __name__ == "__main__":
    unittest.main()
