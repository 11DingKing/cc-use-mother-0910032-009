"""资质到期风险策略（纯函数）。

所有判定只依赖传入的时钟日期 ``today``，不读取仓储，便于单元测试与回放。

风险类别区分：

- ``EXPIRING_SOON``  临期：到期日前 30 天起，WARN
- ``GRACE``          宽限：已过期但续期在途（已提交/待补件/已补件），WARN，
                      有效临时凭证同样给予宽限
- ``SUPPLEMENT_DUE`` 待补件：处于补件要求期；超过补件截止日升级为 BLOCK
- ``LAPSED``         失效：过期且无在途续期（或续期被撤回/驳回），BLOCK

时间语义（BLOCK 类限制 ``effective_from``）：

- 失效：到期日的次日 0 点起生效（到期当日服务仍有依据）；
  续期在途期间顺延为宽限，撤回/驳回当日宽限立即终结，
  自次日起按失效处理（资质本未过期则仍回正常/临期）。
- 补件逾期冻结：补件截止日的次日起生效，补证提交当日解除。
- 监管暂停：暂停起始日当日起生效（暂停命令即时生效），解除日当日起失效。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import (
    LEVEL_BLOCK,
    LEVEL_WARN,
    RENEWAL_PENDING,
    RENEWAL_REJECTED,
    RENEWAL_SUBMITTED,
    RENEWAL_SUPPLEMENT_REQUESTED,
    RENEWAL_SUPPLEMENTED,
    RENEWAL_WITHDRAWN,
    RENEWAL_TERMINAL,
    RISK_EXPIRING_SOON,
    RISK_GRACE,
    RISK_LAPSED,
    RISK_SUPPLEMENT_DUE,
    Qualification,
    RenewalCase,
    TemporaryCredential,
)

EXPIRY_NOTICE_DAYS = 30
"""临期提醒提前天数。"""

GRACE_AFTER_EXPIRY_DAYS = 90
"""续期在途时，宽限在到期日后最多延续的天数；超过即失效。"""


@dataclass(frozen=True)
class RiskVerdict:
    """单日风险判定结果。"""

    category: str | None
    level: str
    detail: str
    # 该风险对应限制的生效日（含）；WARN 类为提示起始日
    effective_from: date
    # 软限制将硬化为 BLOCK 的日期；无则 None
    hardens_on: date | None = None


def active_renewal(cases: list[RenewalCase]) -> RenewalCase | None:
    """挑选当前有效的续期案件：在途案件优先；无在途案件时取最近终局案件。"""
    pending = [c for c in cases if c.status in RENEWAL_PENDING]
    if pending:
        return max(pending, key=lambda c: c.submitted_on)
    terminal = [c for c in cases if c.status in RENEWAL_TERMINAL]
    if terminal:
        decided = [c for c in terminal if c.decided_on is not None]
        pool = decided or terminal
        return max(pool, key=lambda c: c.decided_on or c.withdrawn_on or c.submitted_on)
    return None


def covering_credential(
    creds: list[TemporaryCredential], day: date
) -> TemporaryCredential | None:
    valid = [c for c in creds if c.covers(day)]
    return max(valid, key=lambda c: c.valid_to, default=None)


def evaluate_qualification(
    qual: Qualification,
    cases: list[RenewalCase],
    creds: list[TemporaryCredential],
    today: date,
) -> RiskVerdict | None:
    """按可注入时钟日期评估单项资质的风险；无风险返回 None。

    已核准的续期会反映为资质新的到期日（服务层在核准时写入），
    因此这里只需处理当前 ``expiry_on``。
    """
    expiry = qual.expiry_on
    renewal = active_renewal(cases)
    renewal_pending = renewal is not None and renewal.status in RENEWAL_PENDING
    cred = covering_credential(creds, today)
    grace_limit = expiry + timedelta(days=GRACE_AFTER_EXPIRY_DAYS)

    # 1) 在途续期被要求补件：无论是否已到期都最优先提示
    if renewal_pending and renewal.status == RENEWAL_SUPPLEMENT_REQUESTED \
            and renewal.supplement_deadline:
        deadline = renewal.supplement_deadline
        if today > deadline:
            return RiskVerdict(
                RISK_SUPPLEMENT_DUE,
                LEVEL_BLOCK,
                f"补件截止日 {deadline.isoformat()} 已过，续期待补件冻结，"
                "补交材料后自动解除",
                effective_from=deadline + timedelta(days=1),
            )
        return RiskVerdict(
            RISK_SUPPLEMENT_DUE,
            LEVEL_WARN,
            f"续期核验要求补件，截止日 {deadline.isoformat()}",
            effective_from=renewal.supplement_requested_on or today,
            hardens_on=deadline + timedelta(days=1),
        )

    # 2) 尚未到期 ────────────────────────────────────────────────────────
    if today <= expiry:
        # 续期被撤回/驳回不影响尚未到期的资质
        if expiry - today <= timedelta(days=EXPIRY_NOTICE_DAYS):
            return RiskVerdict(
                RISK_EXPIRING_SOON,
                LEVEL_WARN,
                f"资质将于 {expiry.isoformat()} 到期，请提前办理续期",
                effective_from=expiry - timedelta(days=EXPIRY_NOTICE_DAYS),
            )
        return None

    # 3) 已过期 ──────────────────────────────────────────────────────────
    # 3a) 续期在途（已提交/已补件）：宽限；超过宽限上限则失效
    if renewal_pending:
        if today > grace_limit:
            return _lapsed(
                grace_limit,
                f"续期在途但已超过到期后 {GRACE_AFTER_EXPIRY_DAYS} 天宽限上限",
            )
        return RiskVerdict(
            RISK_GRACE,
            LEVEL_WARN,
            _grace_detail(renewal, cred, expiry, grace_limit),
            effective_from=expiry + timedelta(days=1),
            hardens_on=grace_limit + timedelta(days=1),
        )

    # 3b) 有效临时凭证：宽限（不晚于宽限上限）
    if cred is not None and cred.valid_to <= grace_limit:
        return RiskVerdict(
            RISK_GRACE,
            LEVEL_WARN,
            f"续期临时凭证 {cred.cred_id} 有效至 {cred.valid_to.isoformat()}，"
            "凭证期内可继续执业",
            effective_from=expiry + timedelta(days=1),
            hardens_on=cred.valid_to + timedelta(days=1),
        )

    # 3c) 续期被驳回/撤回，或从未提交：失效
    if renewal is not None and renewal.status in (RENEWAL_WITHDRAWN, RENEWAL_REJECTED):
        ended_on = renewal.withdrawn_on or renewal.decided_on or today
        verb = "撤回" if renewal.status == RENEWAL_WITHDRAWN else "驳回"
        return _lapsed(
            max(expiry, ended_on),
            f"续期案件 {renewal.case_id} 已于 {ended_on.isoformat()} {verb}，"
            "宽限终结且未恢复有效资质",
        )
    return _lapsed(expiry, "资质已过期且无在途续期案件")


def _lapsed(hard_base: date, detail: str) -> RiskVerdict:
    """失效判定。

    ``hard_base`` 为宽限实际存续到的最后一日：无续期时即到期日，
    超宽限上限时为宽限上限日，到期后撤回/驳回时为撤回/驳回当日。
    失效自其次日起生效（当日服务仍处于宽限/有效）。
    """
    return RiskVerdict(
        RISK_LAPSED, LEVEL_BLOCK, detail,
        effective_from=hard_base + timedelta(days=1),
    )


def _grace_detail(
    renewal: RenewalCase | None,
    cred: TemporaryCredential | None,
    expiry: date,
    grace_limit: date,
) -> str:
    status_text = {
        RENEWAL_SUBMITTED: "已提交待核验",
        RENEWAL_SUPPLEMENTED: "补件已提交待复核",
    }
    state = status_text.get(renewal.status, "续期在途")  # type: ignore[union-attr]
    extra = ""
    if cred is not None:
        extra = f"，临时凭证有效至 {cred.valid_to.isoformat()}"
    return (
        f"资质已于 {expiry.isoformat()} 到期，续期案件{state}，"
        f"宽限至 {grace_limit.isoformat()}{extra}；宽限期内可继续执业，"
        "续期驳回/撤回或超期未核准将立即失效"
    )
