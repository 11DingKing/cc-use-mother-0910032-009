"""服务层测试：生命周期、幂等巡检、限制传播与历史留痕。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection.clock import FixedClock
from qualification_inspection.models import (
    ATTENDING_QUALIFICATION,
    LEVEL_BLOCK,
    LEVEL_WARN,
    PRACTICE_LICENSE,
    RESTRICTION_GRACE,
    RESTRICTION_LAPSED,
    RESTRICTION_SUPPLEMENT_HOLD,
    RESTRICTION_SUSPENSION,
)
from qualification_inspection.repository import JsonRepository
from qualification_inspection.service import DomainError, QualificationService


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(date(2026, 1, 1))
        self.svc = QualificationService(clock=self.clock)
        self.svc.register_person("P1", "张三")
        # 执业证 2026-01-01 签发，2026-06-30 到期
        self.svc.register_qualification(
            "Q-LIC", "P1", PRACTICE_LICENSE, "LIC-001",
            date(2026, 1, 1), date(2026, 6, 30))
        self.svc.register_authorization(
            "A1", "P1", "Q-LIC", "门诊出诊", granted_on=date(2026, 1, 1))

    def advance(self, day: str) -> None:
        self.clock.advance_to(day)


class LifecycleTest(ServiceTestBase):
    def test_full_renewal_cycle_clears_risk(self) -> None:
        # 临期
        self.advance("2026-06-10")
        run = self.svc.run_monthly_inspection()
        self.assertEqual(run["risk_total"], 1)
        self.assertEqual(run["created"], 1)
        risks = self.svc.list_risks("P1")
        self.assertEqual(risks[0]["category"], "EXPIRING_SOON")

        # 提交续期 → 宽限
        self.svc.submit_renewal("C1", "Q-LIC")
        self.advance("2026-07-10")
        run = self.svc.run_monthly_inspection()
        risks = self.svc.list_risks("P1")
        self.assertEqual(risks[0]["category"], "GRACE")
        grace = [r for r in self.svc.list_restrictions("P1")
                 if r["type"] == RESTRICTION_GRACE][0]
        self.assertEqual(grace["level"], LEVEL_WARN)
        self.assertEqual(grace["effective_from"], "2026-07-01")

        # 核准 → 风险消除、限制解除、有效期顺延
        self.svc.approve_renewal("C1", date(2027, 6, 30))
        run = self.svc.run_monthly_inspection()
        self.assertEqual(run["cleared"], 1)
        self.assertEqual(self.svc.list_restrictions("P1"), [])
        qual = self.svc.repo.qualifications.require("Q-LIC")
        self.assertEqual(qual.expiry_on, date(2027, 6, 30))

    def test_repeated_inspection_is_idempotent(self) -> None:
        self.advance("2026-06-10")
        first = self.svc.run_monthly_inspection("2026-06")
        second = self.svc.run_monthly_inspection("2026-06")
        self.assertEqual(first["risk_total"], second["risk_total"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["stable"], 1)
        # 风险项仍只有一条
        self.assertEqual(len(self.svc.list_risks("P1")), 1)
        # 巡检运行记录按周期覆盖为一条
        self.assertEqual(len(self.svc.repo.runs.all()), 1)

    def test_supplement_then_block_then_resolve(self) -> None:
        self.svc.submit_renewal("C1", "Q-LIC", submitted_on=date(2026, 6, 20))
        self.advance("2026-07-01")
        self.svc.request_supplement(
            "C1", ["近三年继续教育证明"], date(2026, 7, 10))
        self.svc.run_monthly_inspection()
        warn = [r for r in self.svc.list_restrictions("P1")
                if r["type"] == RESTRICTION_GRACE]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]["level"], LEVEL_WARN)

        # 过截止日仍未补 → 冻结 BLOCK，7/11 生效
        self.advance("2026-07-12")
        self.svc.run_monthly_inspection()
        hold = [r for r in self.svc.list_restrictions("P1", active_on=date(2026, 7, 12))
                if r["type"] == RESTRICTION_SUPPLEMENT_HOLD][0]
        self.assertEqual(hold["level"], LEVEL_BLOCK)
        self.assertEqual(hold["effective_from"], "2026-07-11")
        schedule = self.svc.evaluate_schedule("P1", date(2026, 7, 12))
        self.assertFalse(schedule["allowed"])

        # 补证当日解除
        self.svc.submit_supplement("C1", ["近三年继续教育证明"],
                                   submitted_on=date(2026, 7, 15))
        holds = [r for r in self.svc.list_restrictions("P1")
                 if r["type"] == RESTRICTION_SUPPLEMENT_HOLD]
        self.assertEqual(holds, [])
        # 历史冻结限制保留为非活动，并回填终止日
        old = self.svc.repo.restrictions.get("DR-P1-Q-LIC-SUPPHOLD")
        self.assertFalse(old.active)
        self.assertEqual(old.effective_to, date(2026, 7, 14))
        schedule = self.svc.evaluate_schedule("P1", date(2026, 7, 15))
        self.assertTrue(schedule["allowed"])

    def test_withdraw_after_expiry_lapses(self) -> None:
        self.svc.submit_renewal("C1", "Q-LIC", submitted_on=date(2026, 6, 20))
        self.advance("2026-07-10")
        self.svc.run_monthly_inspection()
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 7, 10))["allowed"])

        self.svc.withdraw_renewal("C1", on=date(2026, 7, 15))
        lapsed = [r for r in self.svc.list_restrictions("P1")
                  if r["type"] == RESTRICTION_LAPSED][0]
        self.assertEqual(lapsed["effective_from"], "2026-07-16")
        # 撤回当日仍可排班（宽限最后一日），次日阻断
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 7, 15))["allowed"])
        self.assertFalse(self.svc.evaluate_schedule("P1", date(2026, 7, 16))["allowed"])

    def test_reject_after_expiry_lapses(self) -> None:
        self.svc.submit_renewal("C1", "Q-LIC", submitted_on=date(2026, 6, 20))
        self.advance("2026-07-05")
        self.svc.reject_renewal("C1", "材料不实", decided_on=date(2026, 7, 5))
        lapsed = [r for r in self.svc.list_restrictions("P1")
                  if r["type"] == RESTRICTION_LAPSED][0]
        self.assertEqual(lapsed["effective_from"], "2026-07-06")

    def test_duplicate_pending_renewal_rejected(self) -> None:
        self.svc.submit_renewal("C1", "Q-LIC")
        with self.assertRaises(DomainError):
            self.svc.submit_renewal("C2", "Q-LIC")

    def test_credential_keeps_grace_until_void(self) -> None:
        self.svc.submit_renewal("C1", "Q-LIC", submitted_on=date(2026, 6, 25))
        self.svc.issue_credential(
            "T1", "C1", date(2026, 7, 1), date(2026, 8, 31),
            issued_on=date(2026, 6, 29))
        self.advance("2026-07-20")
        self.svc.run_monthly_inspection()
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 7, 20))["allowed"])
        self.svc.void_credential("T1", "核验发现疑点", on=date(2026, 7, 18))
        # 续期仍在途 → 仍是宽限（凭证非宽限必要条件）
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 7, 20))["allowed"])
        self.svc.withdraw_renewal("C1", on=date(2026, 7, 19))
        self.assertFalse(self.svc.evaluate_schedule("P1", date(2026, 7, 21))["allowed"])


class SuspensionTest(ServiceTestBase):
    def test_suspension_is_immediate_and_liftable(self) -> None:
        self.svc.suspend_person(
            "S-001", "P1", "监督检查发现违规", date(2026, 3, 1))
        susp = [r for r in self.svc.list_restrictions("P1")
                if r["type"] == RESTRICTION_SUSPENSION][0]
        self.assertEqual(susp["level"], LEVEL_BLOCK)
        self.assertEqual(susp["effective_from"], "2026-03-01")
        self.assertFalse(self.svc.evaluate_schedule("P1", date(2026, 3, 1))["allowed"])
        # 暂停之前的未来排班评估不受影响（但此处授权在 1/1 已授予）
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 2, 28))["allowed"])

        # 巡检记录暂停风险
        self.advance("2026-03-05")
        self.svc.run_monthly_inspection("2026-03")
        self.assertEqual(
            len([r for r in self.svc.list_risks("P1") if r["category"] == "SUSPENDED"]),
            1)

        self.svc.lift_suspension("S-001", lifted_on=date(2026, 3, 10))
        self.assertTrue(self.svc.evaluate_schedule("P1", date(2026, 3, 10))["allowed"])
        old = self.svc.repo.restrictions.get("SUSP-S-001")
        self.assertFalse(old.active)
        self.assertEqual(old.effective_to, date(2026, 3, 10))

        # 再次巡检：暂停风险项关闭
        self.advance("2026-03-15")
        run = self.svc.run_monthly_inspection("2026-03")
        self.assertGreaterEqual(run["cleared"], 1)
        self.assertEqual(
            [r for r in self.svc.list_risks("P1") if r["category"] == "SUSPENDED"],
            [])

    def test_suspension_scope_filters_authorizations(self) -> None:
        self.svc.register_qualification(
            "Q-ATT", "P1", ATTENDING_QUALIFICATION, "ATT-9",
            date(2025, 1, 1), date(2027, 1, 1))
        self.svc.register_authorization(
            "A2", "P1", "Q-ATT", "主诊诊疗", granted_on=date(2025, 1, 1))
        self.svc.suspend_person(
            "S-002", "P1", "主诊资格专项暂停", date(2026, 3, 1),
            scope="主诊诊疗")
        result = self.svc.evaluate_schedule("P1", date(2026, 3, 2))
        by_auth = {a["authorization_id"]: a["allowed"]
                   for a in result["authorizations"]}
        self.assertTrue(by_auth["A1"])
        self.assertFalse(by_auth["A2"])
        self.assertFalse(result["allowed"])

    def test_suspension_risk_is_stable_across_months(self) -> None:
        self.svc.suspend_person("S-003", "P1", "调查", date(2026, 3, 1))
        self.advance("2026-03-15")
        self.svc.run_monthly_inspection("2026-03")
        self.advance("2026-04-15")
        run = self.svc.run_monthly_inspection("2026-04")
        susp_risks = [r for r in self.svc.list_risks("P1")
                      if r["category"] == "SUSPENDED"]
        self.assertEqual(len(susp_risks), 1)
        self.assertGreaterEqual(run["stable"], 1)


class ScheduleAndHistoryTest(ServiceTestBase):
    def test_restrictions_propagate_only_to_future_schedule(self) -> None:
        # 2 月发生的服务已留痕
        self.svc.record_service(
            "SV1", "P1", "A1", date(2026, 2, 10), detail="门诊接诊")
        # 7 月失效
        self.advance("2026-07-15")
        self.svc.run_monthly_inspection()
        self.assertFalse(self.svc.evaluate_schedule("P1", date(2026, 7, 15))["allowed"])

        impact = self.svc.future_schedule_impact(
            "P1", date(2026, 7, 15), date(2026, 12, 31))
        self.assertEqual(len(impact["windows"]), 1)
        window = impact["windows"][0]
        self.assertEqual(window["type"], RESTRICTION_LAPSED)
        self.assertEqual(window["affects_authorizations"], ["A1"])
        self.assertEqual(window["effective_from"], "2026-07-01")

        # 历史服务依据不变，且仍记录当时的到期日快照
        record = self.svc.repo.service_records.require("SV1")
        self.assertEqual(record.qual_expiry_on, date(2026, 6, 30))
        self.assertEqual(record.scope, "门诊出诊")

    def test_warn_does_not_block(self) -> None:
        self.advance("2026-06-20")
        self.svc.run_monthly_inspection()
        result = self.svc.evaluate_schedule("P1", date(2026, 6, 20))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["authorizations"][0]["restrictions"][0]["level"],
                         LEVEL_WARN)

    def test_revoked_authorization_excluded(self) -> None:
        self.svc.revoke_authorization("A1", on=date(2026, 2, 1))
        result = self.svc.evaluate_schedule("P1", date(2026, 2, 2))
        self.assertEqual(result["authorizations"], [])
        self.assertTrue(result["allowed"])


class PersistenceTest(unittest.TestCase):
    def test_json_repo_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo.json"
            clock = FixedClock(date(2026, 6, 10))
            svc = QualificationService(str(path), clock=clock)
            svc.register_person("P1", "张三")
            svc.register_qualification(
                "Q1", "P1", PRACTICE_LICENSE, "L1",
                date(2025, 7, 1), date(2026, 6, 30))
            svc.register_authorization("A1", "P1", "Q1", "门诊")
            svc.run_monthly_inspection()

            svc2 = QualificationService(JsonRepository(path),
                                        clock=FixedClock(date(2026, 6, 10)))
            self.assertEqual(len(svc2.list_risks("P1")), 1)
            self.assertEqual(
                svc2.evaluate_schedule("P1", date(2026, 6, 10))["allowed"], True)


if __name__ == "__main__":
    unittest.main()
