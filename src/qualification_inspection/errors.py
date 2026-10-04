"""领域异常。"""
from __future__ import annotations


class ComplianceError(ValueError):
    """业务规则冲突（状态不允许该操作、硬限制下执业等）。"""


class NotFoundError(LookupError):
    """实体不存在。"""
