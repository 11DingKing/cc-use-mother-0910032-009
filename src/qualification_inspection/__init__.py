"""人员资质到期巡检后端。

核心入口：

- :class:`qualification_inspection.service.QualificationService`：应用服务与命令
- :class:`qualification_inspection.clock.Clock` / :class:`~clock.FixedClock`：可注入时钟
- :class:`qualification_inspection.repository.JsonRepository`：JSON 持久化
"""
from __future__ import annotations

from .clock import Clock, FixedClock, SystemClock
from .models import (
    ATTENDING_QUALIFICATION,
    LEVEL_BLOCK,
    LEVEL_WARN,
    PRACTICE_LICENSE,
    RENEWAL_APPROVED,
    RENEWAL_SUBMITTED,
    RENEWAL_SUPPLEMENT_REQUESTED,
    RENEWAL_SUPPLEMENTED,
    RENEWAL_WITHDRAWN,
    RISK_EXPIRING_SOON,
    RISK_GRACE,
    RISK_LAPSED,
    RISK_SUPPLEMENT_DUE,
    RISK_SUSPENDED,
)
from .repository import JsonRepository, Repository
from .service import DomainError, QualificationService

__all__ = [
    "Clock",
    "FixedClock",
    "SystemClock",
    "JsonRepository",
    "Repository",
    "QualificationService",
    "DomainError",
    "PRACTICE_LICENSE",
    "ATTENDING_QUALIFICATION",
    "LEVEL_BLOCK",
    "LEVEL_WARN",
    "RENEWAL_SUBMITTED",
    "RENEWAL_SUPPLEMENT_REQUESTED",
    "RENEWAL_SUPPLEMENTED",
    "RENEWAL_APPROVED",
    "RENEWAL_WITHDRAWN",
    "RISK_EXPIRING_SOON",
    "RISK_SUPPLEMENT_DUE",
    "RISK_GRACE",
    "RISK_LAPSED",
    "RISK_SUSPENDED",
]
