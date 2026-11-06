"""Clip lifecycle state machine.

All status changes of a clip go through :func:`transition`. It validates the
transition, runs guards and writes an audit log entry in the same session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlmodel import Session, col, select

from clipforge.core.compliance_report import ComplianceReport
from clipforge.core.models import Actor, AuditLog, ClipCandidate, ClipStatus, RenderedClip, utcnow

S = ClipStatus

TRANSITIONS: Mapping[ClipStatus, frozenset[ClipStatus]] = {
    S.CANDIDATE: frozenset({S.RENDERED, S.REJECTED, S.FAILED}),
    S.RENDERED: frozenset({S.PENDING_REVIEW, S.FAILED}),
    S.PENDING_REVIEW: frozenset({S.APPROVED, S.REJECTED, S.RENDERED}),  # RENDERED = re-render
    S.APPROVED: frozenset({S.SCHEDULED, S.FAILED}),
    S.REJECTED: frozenset({S.CLOSED}),
    S.SCHEDULED: frozenset({S.PUBLISHED, S.APPROVED, S.FAILED}),  # APPROVED = unscheduled
    S.PUBLISHED: frozenset({S.SUBMITTED, S.TRACKING}),
    S.SUBMITTED: frozenset({S.TRACKING}),
    S.TRACKING: frozenset({S.CLOSED}),
    S.FAILED: frozenset({S.CANDIDATE, S.CLOSED}),  # CANDIDATE = retry
    S.CLOSED: frozenset(),
}


class TransitionError(Exception):
    """Raised when a status change is not allowed."""


def latest_render(session: Session, clip: ClipCandidate) -> RenderedClip | None:
    stmt = (
        select(RenderedClip)
        .where(RenderedClip.candidate_id == clip.id)
        .order_by(col(RenderedClip.id).desc())
    )
    return session.exec(stmt).first()


def _require_compliance_report(session: Session, clip: ClipCandidate, actor: Actor) -> None:
    render = latest_render(session, clip)
    if render is None or render.compliance_report is None:
        raise TransitionError("clip has no compliance report; run the compliance check first")


def _guard_approve(session: Session, clip: ClipCandidate, actor: Actor) -> None:
    if clip.status is ClipStatus.SCHEDULED:
        return  # unscheduling an already approved clip is not a new approval
    if actor is Actor.SYSTEM:
        raise TransitionError("approval requires a reviewer (agent or human), not the system")
    render = latest_render(session, clip)
    if render is None or render.compliance_report is None:
        raise TransitionError("clip has no compliance report")
    report = ComplianceReport.model_validate(render.compliance_report)
    if not report.passed:
        rules = ", ".join(v.rule for v in report.hard_violations) or "no checks run"
        raise TransitionError(f"compliance check not passed: {rules}")
    if actor is Actor.AGENT:
        review = render.agent_review or {}
        if review.get("decision") != "approve":
            raise TransitionError("agent approval requires a recorded agent review with 'approve'")


Guard = Callable[[Session, ClipCandidate, Actor], None]

GUARDS: Mapping[ClipStatus, Guard] = {
    S.PENDING_REVIEW: _require_compliance_report,
    S.APPROVED: _guard_approve,
}


def can_transition(from_status: ClipStatus, to_status: ClipStatus) -> bool:
    return to_status in TRANSITIONS[from_status]


def transition(
    session: Session,
    clip: ClipCandidate,
    to_status: ClipStatus,
    *,
    actor: Actor,
    reason: str = "",
) -> AuditLog:
    """Move `clip` to `to_status` after validation. Does not commit."""
    if clip.id is None:
        raise TransitionError("clip must be persisted before changing status")
    from_status = clip.status
    if not can_transition(from_status, to_status):
        raise TransitionError(f"transition {from_status} -> {to_status} is not allowed")
    if to_status is S.REJECTED and not reason:
        raise TransitionError("rejecting a clip requires a reason")
    guard = GUARDS.get(to_status)
    if guard is not None:
        guard(session, clip, actor)

    clip.status = to_status
    clip.updated_at = utcnow()
    if to_status is S.FAILED:
        clip.error = reason or clip.error
    entry = AuditLog(
        entity_type="clip",
        entity_id=clip.id,
        from_status=from_status.value,
        to_status=to_status.value,
        actor=actor,
        reason=reason,
    )
    session.add(clip)
    session.add(entry)
    return entry
