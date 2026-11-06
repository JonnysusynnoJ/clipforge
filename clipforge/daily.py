"""Helpers for the unattended daily run: status overview and Metricool reconciliation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from clipforge.campaigns.service import is_ready
from clipforge.core.models import (
    Actor,
    Campaign,
    CampaignStatus,
    ClipCandidate,
    ClipStatus,
    Post,
    PostStatus,
    RenderedClip,
    SocialAccount,
    SourceAsset,
)
from clipforge.core.settings import Settings
from clipforge.core.state import transition

BACKLOG_TARGET = 3  # approved-but-unposted clips to keep ready per campaign
PUBLISHED_GRACE = timedelta(minutes=30)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _campaign_of_clip(session: Session, clip: ClipCandidate) -> int | None:
    asset = session.get(SourceAsset, clip.asset_id)
    return asset.campaign_id if asset else None


def status(session: Session, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    clips = session.exec(select(ClipCandidate)).all()
    by_campaign: dict[int, dict[str, int]] = {}
    for c in clips:
        cid = _campaign_of_clip(session, c)
        if cid is None:
            continue
        counts = by_campaign.setdefault(cid, {})
        counts[c.status.value] = counts.get(c.status.value, 0) + 1

    campaigns = []
    for camp in session.exec(
        select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE)
    ).all():
        counts = by_campaign.get(camp.id or 0, {})
        approved = counts.get("approved", 0)
        assets = session.exec(select(SourceAsset).where(SourceAsset.campaign_id == camp.id)).all()
        used_moments = sum(
            1
            for x in clips
            if _campaign_of_clip(session, x) == camp.id
            and x.status not in (ClipStatus.CANDIDATE, ClipStatus.REJECTED, ClipStatus.FAILED)
        )
        campaigns.append(
            {
                "id": camp.id,
                "title": camp.title or camp.brand,
                "ready": is_ready(camp),
                "budget_left": camp.budget_left,
                "runway_days": (camp.score_report or {}).get("runway_days"),
                "assets": [
                    {
                        "id": a.id,
                        "local": bool(a.local_path and Path(a.local_path).exists()),
                        "duration": a.duration,
                    }
                    for a in assets
                ],
                "clip_counts": counts,
                "approved_unposted": approved,
                "used_moments": used_moments,
                "produce_clips": bool(assets) and approved < BACKLOG_TARGET,
                "needs_ingest": not assets,
            }
        )

    posts = session.exec(select(Post)).all()
    open_posts = [
        {
            "post_id": p.id,
            "status": p.status.value,
            "at": _aware(p.scheduled_at).isoformat(),
            "metricool_id": p.metricool_post_id,
            "draft": p.draft,
        }
        for p in posts
        if p.status in (PostStatus.PLANNED, PostStatus.SCHEDULED)
    ]
    accounts = [
        {
            "id": a.id,
            "platform": a.platform.value,
            "handle": a.handle,
            "whop_linked": a.whop_linked,
            "blog_id": a.metricool_blog_id,
        }
        for a in session.exec(select(SocialAccount).where(SocialAccount.active == True)).all()  # noqa: E712
    ]
    return {
        "now": now.isoformat(),
        "publishing": {"enabled": settings.publishing.enabled, "mode": settings.publishing.mode},
        "rate_limit": settings.rate_limits.model_dump(),
        "active_campaigns": campaigns,
        "open_posts": open_posts,
        "accounts": accounts,
        "brands_to_sync": sorted({a["blog_id"] for a in accounts if a["blog_id"]}),
    }


def _uuid(post: Post) -> str | None:
    m = re.search(r"openWithPostUuid=(\d+)", post.planner_url or "")
    return m.group(1) if m else None


def _url(provider: dict[str, Any]) -> str | None:
    for k in ("publicUrl", "url", "link", "permalink", "postUrl"):
        v = provider.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def sync_scheduled(
    session: Session, scheduled: dict[str, Any] | list[Any], now: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Reconcile SCHEDULED posts with Metricool's getScheduledPosts answer(s).

    `scheduled` is the raw tool output (or a list of them, one per brand). A post that is
    past its time and no longer listed as pending is treated as published (URL filled in
    later from analytics); provider status ERROR marks it failed.
    """
    now = now or datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for block in scheduled if isinstance(scheduled, list) else [scheduled]:
        items += block.get("data", []) if isinstance(block, dict) else []
    by_uuid = {str(i.get("uuid")): i for i in items}
    by_id = {str(i.get("id")): i for i in items}
    out: dict[str, list[dict[str, Any]]] = {"published": [], "failed": [], "pending": []}
    for post in session.exec(select(Post).where(Post.status == PostStatus.SCHEDULED)).all():
        if post.draft:
            continue
        item = by_uuid.get(_uuid(post) or "") or by_id.get(post.metricool_post_id or "")
        providers = item.get("providers", []) if item else []
        states = {str(p.get("status", "")).upper() for p in providers}
        clip = _clip(session, post)
        if item and states & {"ERROR", "FAILED", "REJECTED"}:
            detail = "; ".join(str(p.get("detailedStatus")) for p in providers)
            post.status = PostStatus.FAILED
            post.error = f"Metricool: {detail}"[:1000]
            if clip.status is ClipStatus.SCHEDULED:
                transition(
                    session,
                    clip,
                    ClipStatus.APPROVED,
                    actor=Actor.SYSTEM,
                    reason=f"publish failed: {detail}"[:500],
                )
            out["failed"].append({"post_id": post.id, "error": post.error})
        elif item and not states & {"PUBLISHED", "SUCCESS", "DONE"}:
            out["pending"].append({"post_id": post.id, "at": _aware(post.scheduled_at).isoformat()})
            continue
        elif item or _aware(post.scheduled_at) + PUBLISHED_GRACE < now:
            url = next((u for p in providers if (u := _url(p))), None)
            post.status = PostStatus.PUBLISHED
            post.published_url = url
            if clip.status is ClipStatus.SCHEDULED:
                transition(
                    session,
                    clip,
                    ClipStatus.PUBLISHED,
                    actor=Actor.SYSTEM,
                    reason=url or "published (url pending)",
                )
            out["published"].append({"post_id": post.id, "url": url})
        else:
            out["pending"].append({"post_id": post.id, "at": _aware(post.scheduled_at).isoformat()})
            continue
        session.add(post)
    return out


def _clip(session: Session, post: Post) -> ClipCandidate:
    render = session.get(RenderedClip, post.rendered_clip_id)
    clip = session.get(ClipCandidate, render.candidate_id) if render else None
    if clip is None:
        raise ValueError(f"post {post.id} has no clip")
    return clip


def posts_missing_url(session: Session) -> list[dict[str, Any]]:
    rows = session.exec(
        select(Post).where(Post.status == PostStatus.PUBLISHED, col(Post.published_url).is_(None))
    ).all()
    return [
        {"post_id": p.id, "at": _aware(p.scheduled_at).isoformat(), "caption": p.caption[:60]}
        for p in rows
    ]
