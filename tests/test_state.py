from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, select

from clipforge.core.compliance_report import ComplianceReport, Severity, Violation
from clipforge.core.models import Actor, AuditLog, ClipCandidate, ClipStatus, RenderedClip
from clipforge.core.state import TRANSITIONS, TransitionError, can_transition, transition

S = ClipStatus


def _render(
    session: Session,
    clip: ClipCandidate,
    report: ComplianceReport | None,
    review: dict[str, Any] | None = None,
) -> RenderedClip:
    r = RenderedClip(
        candidate_id=clip.id or 0,
        path="data/renders/x.mp4",
        platform_profile="tiktok",
        compliance_report=report.model_dump(mode="json") if report else None,
        agent_review=review,
    )
    session.add(r)
    session.commit()
    return r


PASSING = ComplianceReport(checks_run=["length", "disclosure"])
FAILING = ComplianceReport(
    checks_run=["disclosure"],
    violations=[Violation(rule="disclosure", severity=Severity.HARD, message="missing #ad")],
)


def _to_pending(session: Session, clip: ClipCandidate, review: dict[str, Any] | None) -> None:
    transition(session, clip, S.RENDERED, actor=Actor.SYSTEM)
    _render(session, clip, PASSING, review)
    transition(session, clip, S.PENDING_REVIEW, actor=Actor.SYSTEM)


def test_every_status_has_transition_entry() -> None:
    assert set(TRANSITIONS) == set(ClipStatus)


def test_closed_is_terminal() -> None:
    assert all(not can_transition(S.CLOSED, s) for s in ClipStatus)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (S.CANDIDATE, S.APPROVED),
        (S.CANDIDATE, S.PUBLISHED),
        (S.RENDERED, S.APPROVED),
        (S.PENDING_REVIEW, S.SCHEDULED),
        (S.REJECTED, S.APPROVED),
        (S.PUBLISHED, S.CANDIDATE),
    ],
)
def test_forbidden_shortcuts(src: ClipStatus, dst: ClipStatus) -> None:
    assert not can_transition(src, dst)


def test_happy_path_with_agent_approval(session: Session, clip: ClipCandidate) -> None:
    _to_pending(session, clip, {"decision": "approve", "score": 8.1})
    transition(session, clip, S.APPROVED, actor=Actor.AGENT, reason="strong hook")
    for nxt in (S.SCHEDULED, S.PUBLISHED, S.SUBMITTED, S.TRACKING, S.CLOSED):
        transition(session, clip, nxt, actor=Actor.SYSTEM)
    session.commit()
    assert clip.status is S.CLOSED
    log = session.exec(select(AuditLog).where(AuditLog.entity_id == clip.id)).all()
    assert [e.to_status for e in log] == [
        "rendered",
        "pending_review",
        "approved",
        "scheduled",
        "published",
        "submitted",
        "tracking",
        "closed",
    ]
    assert log[2].actor is Actor.AGENT


def test_invalid_transition_raises_and_leaves_status(session: Session, clip: ClipCandidate) -> None:
    with pytest.raises(TransitionError):
        transition(session, clip, S.PUBLISHED, actor=Actor.SYSTEM)
    assert clip.status is S.CANDIDATE
    assert session.exec(select(AuditLog)).all() == []


def test_pending_review_requires_compliance_report(session: Session, clip: ClipCandidate) -> None:
    transition(session, clip, S.RENDERED, actor=Actor.SYSTEM)
    _render(session, clip, report=None)
    with pytest.raises(TransitionError, match="compliance report"):
        transition(session, clip, S.PENDING_REVIEW, actor=Actor.SYSTEM)


def test_system_can_never_approve(session: Session, clip: ClipCandidate) -> None:
    _to_pending(session, clip, {"decision": "approve"})
    with pytest.raises(TransitionError, match="reviewer"):
        transition(session, clip, S.APPROVED, actor=Actor.SYSTEM)


def test_hard_violation_blocks_approval_even_for_human(
    session: Session, clip: ClipCandidate
) -> None:
    _to_pending(session, clip, {"decision": "approve"})
    _render(session, clip, FAILING, {"decision": "approve"})  # newest render fails compliance
    for actor in (Actor.AGENT, Actor.HUMAN):
        with pytest.raises(TransitionError, match="disclosure"):
            transition(session, clip, S.APPROVED, actor=actor)


def test_agent_approval_needs_recorded_approve_decision(
    session: Session, clip: ClipCandidate
) -> None:
    _to_pending(session, clip, {"decision": "reject"})
    with pytest.raises(TransitionError, match="agent review"):
        transition(session, clip, S.APPROVED, actor=Actor.AGENT)


def test_human_can_approve_without_agent_review(session: Session, clip: ClipCandidate) -> None:
    _to_pending(session, clip, review=None)
    transition(session, clip, S.APPROVED, actor=Actor.HUMAN)
    assert clip.status is S.APPROVED


def test_reject_requires_reason(session: Session, clip: ClipCandidate) -> None:
    with pytest.raises(TransitionError, match="reason"):
        transition(session, clip, S.REJECTED, actor=Actor.AGENT)
    transition(session, clip, S.REJECTED, actor=Actor.AGENT, reason="duplicate moment")
    assert clip.status is S.REJECTED


def test_failed_records_error_and_can_retry(session: Session, clip: ClipCandidate) -> None:
    transition(session, clip, S.FAILED, actor=Actor.SYSTEM, reason="ffmpeg exit 1")
    assert clip.error == "ffmpeg exit 1"
    transition(session, clip, S.CANDIDATE, actor=Actor.SYSTEM, reason="retry")
    assert clip.status is S.CANDIDATE


def test_unpersisted_clip_rejected(session: Session) -> None:
    with pytest.raises(TransitionError, match="persisted"):
        transition(
            session, ClipCandidate(asset_id=1, start=0, end=1), S.RENDERED, actor=Actor.SYSTEM
        )


def test_unschedule_returns_to_approved_without_new_review(
    session: Session, clip: ClipCandidate
) -> None:
    _to_pending(session, clip, {"decision": "approve"})
    transition(session, clip, S.APPROVED, actor=Actor.AGENT)
    transition(session, clip, S.SCHEDULED, actor=Actor.SYSTEM)
    transition(session, clip, S.APPROVED, actor=Actor.SYSTEM, reason="post cancelled")
    assert clip.status is S.APPROVED
