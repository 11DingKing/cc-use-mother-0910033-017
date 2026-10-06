"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误，HTTP 层映射为 4xx。"""

    code = "domain_error"
    status = 400


class NotFound(DomainError):
    code = "not_found"
    status = 404


class Conflict(DomainError):
    code = "conflict"
    status = 409


class ValidationFailed(DomainError):
    code = "validation_failed"
    status = 422


class CycleDetected(DomainError):
    code = "cycle_detected"
    status = 409

    def __init__(self, message: str, cycles: list[dict] | None = None):
        super().__init__(message)
        self.cycles = cycles or []
