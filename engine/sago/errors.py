"""Typed failures. Every agent raises one of these, and the Conductor routes
each type to one response, so the same fault always gets the same handling.

    TransientError      retried with backoff, then dead lettered
    RateLimited         backoff and slow the pacing for this run
    LocalBlock          the site answers everyone except us: pause writes, alert
    SiteUnreachable     down from every vantage point: skip the run, alert
    AuthError           credentials rejected: open that adapter's breaker
    PolicyViolation     the action is killed, never retried
    VerificationFailed  the change is rolled back and its kind quarantined
    RollbackFailed      global halt: the only fault that needs a human
"""
from __future__ import annotations


class SagoError(Exception):
    retryable = False


class TransientError(SagoError):
    retryable = True


class RateLimited(TransientError):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class SiteUnreachable(SagoError):
    pass


class LocalBlock(SagoError):
    """Our own IP cannot reach the site while external vantage points can."""


class AuthError(SagoError):
    def __init__(self, adapter: str, msg: str):
        super().__init__(f"{adapter}: {msg}")
        self.adapter = adapter


class PolicyViolation(SagoError):
    pass


class VerificationFailed(SagoError):
    def __init__(self, msg: str, checks: list | None = None):
        super().__init__(msg)
        self.checks = checks or []


class RollbackFailed(SagoError):
    pass
