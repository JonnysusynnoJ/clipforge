"""Compliance report schema shared by the compliance module and the state machine guards."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    HARD = "hard"  # blocks approval and publishing
    SOFT = "soft"  # shown as warning


class Violation(BaseModel):
    rule: str
    severity: Severity
    message: str


class ComplianceReport(BaseModel):
    checks_run: list[str] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)

    @property
    def hard_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.HARD]

    @property
    def passed(self) -> bool:
        return bool(self.checks_run) and not self.hard_violations
