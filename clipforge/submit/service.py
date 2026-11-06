"""Semi-automatic Whop submission: ready-to-paste lists and submission tracking.

Content Rewards has no documented creator API for submissions, so ClipForge prepares the
list (campaign link + post URLs) and records what the owner submitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from clipforge.analytics.earnings import post_campaign
from clipforge.core.models import (
    Actor,
    ClipCandidate,
    ClipStatus,
    Post,
    PostStatus,
    RenderedClip,
    Submission,
)
from clipforge.core.settings import Settings
from clipforge.core.state import transition

WHOP_STATES = ("submitted", "approved", "rejected", "paid")


def _clip(session: Session, post: Post) -> ClipCandidate | None:
    render = session.get(RenderedClip, post.rendered_clip_id)
    return session.get(ClipCandidate, render.candidate_id) if render else None


def pending(session: Session) -> list[dict[str, Any]]:
    """Published posts with a URL that were not submitted yet, grouped by campaign."""
    done = {s.post_id for s in session.exec(select(Submission)).all() if s.submitted_at}
    groups: dict[int, dict[str, Any]] = {}
    for post in session.exec(select(Post).where(Post.status == PostStatus.PUBLISHED)).all():
        if post.id in done or not post.published_url:
            continue
        campaign = post_campaign(session, post)
        if campaign is None or campaign.id is None:
            continue
        g = groups.setdefault(
            campaign.id,
            {
                "campaign_id": campaign.id,
                "campaign": campaign.title or campaign.brand,
                "submit_url": campaign.submit_url or campaign.whop_url,
                "posts": [],
            },
        )
        g["posts"].append(
            {
                "post_id": post.id,
                "url": post.published_url,
                "platform": post.platform.value if post.platform else None,
            }
        )
    return list(groups.values())


def write_sheet(
    settings: Settings, groups: list[dict[str, Any]], now: datetime | None = None
) -> Path:
    now = now or datetime.now(UTC)
    path = settings.paths.data_dir / "reports" / f"submit_{now:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Whop-Einreichung {now:%d.%m.%Y}", ""]
    if not groups:
        lines.append("Nichts einzureichen.")
    for g in groups:
        lines += [f"## {g['campaign']}", "", f"Einreichen: {g['submit_url']}", ""]
        lines += [f"- {p['url']}  (Post {p['post_id']}, {p['platform']})" for p in g["posts"]]
        lines.append("")
    lines += ["Nach dem Einreichen: `clipforge submit-mark <post_id>` (oder dem Agenten sagen)."]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def mark(session: Session, post_id: int, status: str = "submitted", notes: str = "") -> Submission:
    if status not in WHOP_STATES:
        raise ValueError(f"status must be one of {WHOP_STATES}")
    post = session.get(Post, post_id)
    if post is None or post.status is not PostStatus.PUBLISHED:
        raise ValueError(f"post {post_id} is not published")
    campaign = post_campaign(session, post)
    sub = session.exec(select(Submission).where(Submission.post_id == post_id)).first()
    sub = sub or Submission(post_id=post_id, campaign_id=(campaign.id or 0) if campaign else 0)
    sub.submitted_at = sub.submitted_at or datetime.now(UTC)
    sub.whop_status = status
    if notes:
        sub.notes = notes
    session.add(sub)
    clip = _clip(session, post)
    if clip is not None and clip.status is ClipStatus.PUBLISHED:
        transition(
            session,
            clip,
            ClipStatus.SUBMITTED,
            actor=Actor.HUMAN,
            reason=f"submitted to Whop ({status})",
        )
        transition(session, clip, ClipStatus.TRACKING, actor=Actor.SYSTEM, reason="tracking views")
    session.flush()
    return sub
