"""策略层单元测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection.models import (
    LEVEL_BLOCK,
    LEVEL_WARN,
    PRACTICE_LICENSE,
    RenewalCase,
    Qualification,
    RENEWAL_SUBMITTED,
    RENEWAL_SUPPLEMENT_REQUESTED,
    RENEWAL_SUPPLEMENTED,
    RENEWAL_WITHDRAWN,
    RISK_EXPIRING_SOON,
    RISK_GRACE,
    RISK_LAPSED,
    RISK_SUPPLEMENT_DUE,
    TemporaryCredential,
)
from qualification_inspection.policy import (
    EXPIRY_NOTICE_DAYS,
    GRACE_AFTER_EXPIRY_DAYS,
    evaluate_qualification,
)


def qual(expiry: date, issued: date | None = None) -> Qualification:
    return Qualification(
        qual_id="Q1", person_id="P1", qual_type=PRACTICE_LICENSE,
        qual_number="LIC-1", issued_on=issued or expiry - timedelta(days=365),
        expiry_on=expiry,
    )


def case(status: str = RENEWAL_SUBMITTED, submitted: date | None = None,
         deadline: date | None = None, requested: date | None = None,
         withdrawn: date | None = None) -> RenewalCase:
    c = RenewalCase(case_id="C1", qual_id="Q1",
                    submitted_on=submitted or date(2026, 6, 1), status=status)
    if deadline:
        c.supplement_deadline = deadline
        c.supplement_requested_on = requested
    if withdrawn:
        c.withdrawn_on = withdrawn
    return c


class PolicyTest(unittest.TestCase):
    def test_no_risk_when_far_from_expiry(self) -> None:
        expiry = date(2026, 12, 31)
        verdict = evaluate_qualification(qual(expiry), [], [], date(2026, 6, 1))
        self.assertIsNone(verdict)

    def test_expiring_soon_warn(self) -> None:
        expiry = date(2026, 7, 20)
        verdict = evaluate_qualification(qual(expiry), [], [], date(2026, 6, 25))
        assert verdict is not None
        self.assertEqual(verdict.category, RISK_EXPIRING_SOON)
        self.assertEqual(verdict.level, LEVEL_WARN)
        self.assertEqual(verdict.effective_from,
                         expiry - timedelta(days=EXPIRY_NOTICE_DAYS))

    def test_simple_expiry_lapses_next_day(self) -> None:
        expiry = date(2026, 6, 30)
        # 到期当日仍有效，只给临期提示；次日无续期即失效
        on_day = evaluate_qualification(qual(expiry), [], [], expiry)
        assert on_day is not None
        self.assertEqual(on_day.category, RISK_EXPIRING_SOON)
        verdict = evaluate_qualification(qual(expiry), [], [],
                                         expiry + timedelta(days=1))
        assert verdict is not None
        self.assertEqual((verdict.category, verdict.level),
                         (RISK_LAPSED, LEVEL_BLOCK))
        self.assertEqual(verdict.effective_from, date(2026, 7, 1))

    def test_renewal_pending_gives_grace(self) -> None:
        expiry = date(2026, 6, 30)
        c = case(submitted=date(2026, 6, 20))
        verdict = evaluate_qualification(qual(expiry), [c], [], date(2026, 7, 15))
        assert verdict is not None
        self.assertEqual(verdict.category, RISK_GRACE)
        self.assertEqual(verdict.level, LEVEL_WARN)
        self.assertEqual(verdict.effective_from, date(2026, 7, 1))
        self.assertEqual(verdict.hardens_on,
                         expiry + timedelta(days=GRACE_AFTER_EXPIRY_DAYS + 1))

    def test_grace_hard_cap_lapses(self) -> None:
        expiry = date(2026, 6, 30)
        c = case(RENEWAL_SUPPLEMENTED)
        limit = expiry + timedelta(days=GRACE_AFTER_EXPIRY_DAYS)
        verdict = evaluate_qualification(qual(expiry), [c], [],
                                         limit + timedelta(days=1))
        assert verdict is not None
        self.assertEqual(verdict.category, RISK_LAPSED)
        # 回溯到宽限上限次日生效，而非巡检当天
        self.assertEqual(verdict.effective_from, limit + timedelta(days=1))

    def test_supplement_warn_then_block_after_deadline(self) -> None:
        expiry = date(2026, 6, 30)
        deadline = date(2026, 7, 10)
        c = case(RENEWAL_SUPPLEMENT_REQUESTED, deadline=deadline,
                 requested=date(2026, 7, 1))
        warn = evaluate_qualification(qual(expiry), [c], [], date(2026, 7, 5))
        assert warn is not None
        self.assertEqual(warn.category, RISK_SUPPLEMENT_DUE)
        self.assertEqual(warn.level, LEVEL_WARN)
        self.assertEqual(warn.hardens_on, date(2026, 7, 11))

        blocked = evaluate_qualification(qual(expiry), [c], [], date(2026, 7, 11))
        assert blocked is not None
        self.assertEqual(blocked.level, LEVEL_BLOCK)
        self.assertEqual(blocked.effective_from, date(2026, 7, 11))

    def test_withdraw_after_expiry_lapses_next_day(self) -> None:
        expiry = date(2026, 6, 30)
        c = case(RENEWAL_WITHDRAWN, withdrawn=date(2026, 7, 15))
        verdict = evaluate_qualification(qual(expiry), [c], [], date(2026, 7, 20))
        assert verdict is not None
        self.assertEqual(verdict.category, RISK_LAPSED)
        # 撤回当日宽限终结，次日失效
        self.assertEqual(verdict.effective_from, date(2026, 7, 16))

    def test_withdraw_before_expiry_no_lapse(self) -> None:
        expiry = date(2026, 12, 31)
        c = case(RENEWAL_WITHDRAWN, withdrawn=date(2026, 6, 10))
        self.assertIsNone(
            evaluate_qualification(qual(expiry), [c], [], date(2026, 6, 15)))

    def test_covering_credential_grants_grace(self) -> None:
        expiry = date(2026, 6, 30)
        cred = TemporaryCredential(
            cred_id="T1", case_id="C1", qual_id="Q1",
            valid_from=date(2026, 7, 1), valid_to=date(2026, 8, 31),
            issued_on=date(2026, 6, 28))
        verdict = evaluate_qualification(qual(expiry), [], [cred], date(2026, 7, 20))
        assert verdict is not None
        self.assertEqual(verdict.category, RISK_GRACE)
        # 凭证作废后不再覆盖
        cred.voided_on = date(2026, 7, 10)
        lapsed = evaluate_qualification(qual(expiry), [], [cred], date(2026, 7, 20))
        assert lapsed is not None
        self.assertEqual(lapsed.category, RISK_LAPSED)


if __name__ == "__main__":
    unittest.main()
