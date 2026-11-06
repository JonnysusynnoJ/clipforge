"""Turn approved clips into posts: guards, slots, media upload, publisher handoff."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from clipforge.campaigns.rules import ParsedRules
from clipforge.compliance.check import PostMetadata
from clipforge.core.compliance_report import ComplianceReport
from clipforge.core.logging import get_logger
from clipforge.core.models import (
    Actor,
    Campaign,
    CampaignStatus,
    ClipCandidate,
    ClipStatus,
    Platform,
    Post,
    PostStatus,
    RenderedClip,
    SocialAccount,
    SourceAsset,
)
from clipforge.core.settings import Settings
from clipforge.core.state import latest_render, transition
from clipforge.media import sha256_file
from clipforge.pipeline import compliance_for
from clipforge.publish.hosting import MediaHost, R2Host
from clipforge.publish.metricool import build_request
from clipforge.publish.scheduler import next_slot

log = get_logger(__name__)

ACTIVE_POST_STATES = (PostStatus.PLANNED, PostStatus.SCHEDULED, PostStatus.PUBLISHED)


class PublishBlockedError(Exception):
    pass


def media_host(settings: Settings) -> MediaHost:
    h = settings.hosting
    if h.provider != "r2" or not (h.r2_account_id and h.r2_bucket and h.public_base_url):
        raise PublishBlockedError("hosting not configured (hosting.r2_* in config.yaml)")
    return R2Host(h.r2_account_id, h.r2_bucket, h.public_base_url)


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; ClipForge always stores UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def account_posts(session: Session, account_id: int) -> list[Post]:
    return list(
        session.exec(
            select(Post).where(
                Post.account_id == account_id,
                col(Post.status).in_(ACTIVE_POST_STATES),
            )
        ).all()
    )


def _context(session: Session, clip: ClipCandidate) -> tuple[RenderedClip, Campaign, ParsedRules]:
    render = latest_render(session, clip)
    asset = session.get(SourceAsset, clip.asset_id)
    if render is None or asset is None:
        raise PublishBlockedError("clip has no render")
    campaign = session.get(Campaign, asset.campaign_id)
    if campaign is None:
        raise PublishBlockedError("clip has no campaign")
    return render, campaign, ParsedRules.model_validate(campaign.parsed_rules)


def eligible_accounts(
    session: Session, platforms: list[Platform], rules: ParsedRules, *, need_metricool: bool
) -> list[SocialAccount]:
    allowed = set(platforms) & (set(rules.allowed_platforms) or set(platforms))
    out = []
    for a in session.exec(select(SocialAccount).where(SocialAccount.active == True)).all():  # noqa: E712
        if a.platform not in allowed:
            continue
        if not a.whop_linked or not a.is_public:
            continue  # views would not count for Whop
        if need_metricool and not a.metricool_blog_id:
            continue
        out.append(a)
    return out


def guard_clip(
    session: Session, settings: Settings, clip: ClipCandidate
) -> tuple[RenderedClip, Campaign, ParsedRules, PostMetadata]:
    """All hard checks before a clip may be scheduled. Raises PublishBlockedError."""
    if not settings.publishing.enabled:
        raise PublishBlockedError("kill switch: publishing.enabled is false")
    if clip.status is not ClipStatus.APPROVED:
        raise PublishBlockedError(f"clip is {clip.status.value}, not approved")
    render, campaign, rules = _context(session, clip)
    if campaign.status is not CampaignStatus.ACTIVE:
        raise PublishBlockedError(f"campaign is {campaign.status.value}")
    existing = session.exec(
        select(Post).where(
            Post.rendered_clip_id == render.id, col(Post.status).in_(ACTIVE_POST_STATES)
        )
    ).first()
    if existing is not None:
        raise PublishBlockedError(f"clip already has post {existing.id} (one account per video)")
    report: ComplianceReport = compliance_for(session, settings, clip, render)  # re-check now
    if not report.passed:
        raise PublishBlockedError(
            "compliance: " + "; ".join(v.message for v in report.hard_violations)
        )
    meta = PostMetadata.model_validate(render.post_metadata)
    options = [o.lower() for o in rules.disclosure_options]
    if not any(o in meta.caption.lower() for o in options):
        raise PublishBlockedError("no disclosure in caption")
    return render, campaign, rules, meta


def plan_posts(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    host: MediaHost | None = None,
    preferred_hours: dict[str, list[int]] | None = None,
    limit: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Schedule approved clips: pick account + slot, upload media, create Post (PLANNED)."""
    now = now or datetime.now(UTC)
    pub = settings.publishing
    out: dict[str, list[dict[str, Any]]] = {"planned": [], "blocked": [], "expired": []}
    # planned posts that were never submitted and whose slot is (almost) over: free them
    for stale in session.exec(select(Post).where(Post.status == PostStatus.PLANNED)).all():
        if _aware(stale.scheduled_at) < now + timedelta(minutes=10):
            stale.status = PostStatus.CANCELLED
            stale.error = "expired: never submitted to Metricool"
            session.add(stale)
            out["expired"].append({"post_id": stale.id})
    session.flush()
    clips = session.exec(
        select(ClipCandidate)
        .where(ClipCandidate.status == ClipStatus.APPROVED)
        .order_by(col(ClipCandidate.llm_score).desc())
    ).all()
    for clip in clips[:limit]:
        try:
            render, campaign, rules, meta = guard_clip(session, settings, clip)
            accounts = eligible_accounts(
                session, meta.platforms or rules.allowed_platforms, rules, need_metricool=True
            )
            if not accounts:
                raise PublishBlockedError(
                    "no eligible account (platform, whop_linked, public, metricool)"
                )
            options = []
            for acc in accounts:
                limit_per_day = min(
                    settings.rate_limits.max_posts_per_account_per_day,
                    acc.daily_limit or 10**6,
                )
                slot = next_slot(
                    now=now,
                    taken=[_aware(p.scheduled_at) for p in account_posts(session, acc.id or 0)],
                    daily_limit=limit_per_day,
                    min_gap=timedelta(minutes=settings.rate_limits.min_gap_minutes),
                    tz=pub.timezone,
                    window_start=time(pub.window_start_hour),
                    window_end=time(pub.window_end_hour - 1, 59)
                    if pub.window_end_hour == 24
                    else time(pub.window_end_hour),
                    lead=timedelta(minutes=pub.lead_minutes),
                    preferred_hours=(preferred_hours or {}).get(acc.platform.value),
                )
                options.append((slot, acc))
            slot, account = min(options, key=lambda o: o[0])
            host = host or media_host(settings)
            video = Path(render.path)
            key = f"clips/{campaign.id}/{clip.id}-{sha256_file(video)[:10]}.mp4"
            url = host.upload(video, key)
            if not host.verify(url):
                raise PublishBlockedError(f"uploaded media not publicly reachable: {url}")
            disclosure = next(
                o for o in rules.disclosure_options if o.lower() in meta.caption.lower()
            )
            request = build_request(
                blog_id=account.metricool_blog_id or "",
                platform=account.platform,
                when=slot,
                timezone=pub.timezone,
                caption=meta.caption,
                media_url=url,
                draft=pub.mode == "draft",
                paid=True,
            )
            post = Post(
                rendered_clip_id=render.id or 0,
                account_id=account.id or 0,
                platform=account.platform,
                scheduled_at=slot.astimezone(UTC),  # SQLite drops tzinfo: store UTC
                caption=meta.caption,
                disclosure_text=disclosure,
                media_url=url,
                media_key=key,
                draft=pub.mode == "draft",
                request_payload=request,
            )
            session.add(post)
            session.flush()
            out["planned"].append(
                {
                    "post_id": post.id,
                    "clip_id": clip.id,
                    "account": account.handle,
                    "platform": account.platform.value,
                    "at": slot.isoformat(),
                    "draft": post.draft,
                }
            )
        except PublishBlockedError as exc:
            out["blocked"].append({"clip_id": clip.id, "why": str(exc)})
            if "kill switch" in str(exc):
                break
    return out


def queue(session: Session) -> list[dict[str, Any]]:
    """Requests the agent still has to submit to Metricool."""
    posts = session.exec(
        select(Post).where(Post.status == PostStatus.PLANNED, Post.publisher == "metricool")
    ).all()
    return [{"post_id": p.id, **p.request_payload} for p in posts]


def _clip_of(session: Session, post: Post) -> ClipCandidate:
    render = session.get(RenderedClip, post.rendered_clip_id)
    clip = session.get(ClipCandidate, render.candidate_id) if render else None
    if clip is None:
        raise ValueError(f"post {post.id} has no clip")
    return clip


def record_result(
    session: Session,
    post_id: int,
    *,
    ok: bool,
    metricool_post_id: str | None = None,
    planner_url: str | None = None,
    error: str | None = None,
) -> Post:
    post = session.get(Post, post_id)
    if post is None or post.status is not PostStatus.PLANNED:
        raise ValueError(f"post {post_id} is not waiting for a publisher result")
    if ok:
        post.status = PostStatus.SCHEDULED
        post.metricool_post_id = metricool_post_id
        post.planner_url = planner_url
        kind = "draft" if post.draft else "scheduled"
        transition(
            session,
            _clip_of(session, post),
            ClipStatus.SCHEDULED,
            actor=Actor.SYSTEM,
            reason=f"post {post.id} {kind} in Metricool",
        )
    else:
        post.status = PostStatus.FAILED
        post.error = (error or "unknown error")[:1000]
    session.add(post)
    return post


def mark_published(session: Session, post_id: int, url: str) -> Post:
    post = session.get(Post, post_id)
    if post is None or post.status is not PostStatus.SCHEDULED:
        raise ValueError(f"post {post_id} is not scheduled")
    post.status = PostStatus.PUBLISHED
    post.published_url = url
    clip = _clip_of(session, post)
    if clip.status is ClipStatus.SCHEDULED:
        transition(session, clip, ClipStatus.PUBLISHED, actor=Actor.SYSTEM, reason=url)
    session.add(post)
    return post


def cancel(session: Session, post_id: int, reason: str) -> Post:
    post = session.get(Post, post_id)
    if post is None or post.status not in (PostStatus.PLANNED, PostStatus.SCHEDULED):
        raise ValueError(f"post {post_id} cannot be cancelled")
    was_scheduled = post.status is PostStatus.SCHEDULED
    post.status = PostStatus.CANCELLED
    post.error = reason
    clip = _clip_of(session, post)
    if was_scheduled and clip.status is ClipStatus.SCHEDULED:
        transition(session, clip, ClipStatus.APPROVED, actor=Actor.SYSTEM, reason=reason)
    session.add(post)
    return post


def export_manual(session: Session, settings: Settings, post_id: int) -> Path:
    """ManualExportPublisher: video + caption into data/exports for posting by hand."""
    post = session.get(Post, post_id)
    if post is None:
        raise ValueError(f"post {post_id} not found")
    render = session.get(RenderedClip, post.rendered_clip_id)
    account = session.get(SocialAccount, post.account_id)
    assert render is not None and account is not None
    target = (
        settings.paths.exports
        / f"{account.platform.value}_{account.handle}"
        / f"{_aware(post.scheduled_at):%Y%m%d-%H%M}_post{post.id}"
    )
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(render.path, target / "video.mp4")
    (target / "caption.txt").write_text(post.caption, encoding="utf-8")
    (target / "post.json").write_text(
        json.dumps(
            {
                "post_id": post.id,
                "account": account.handle,
                "platform": account.platform.value,
                "publish_at": _aware(post.scheduled_at).isoformat(),
                "disclosure": post.disclosure_text,
            },
            indent=2,
        )
    )
    post.publisher = "manual"
    session.add(post)
    return target
