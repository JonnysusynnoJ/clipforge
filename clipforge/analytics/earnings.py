"""Earnings estimate per post and campaign: views x CPM, minimum payout, cap, fee."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from clipforge.analytics.metrics import latest_snapshots
from clipforge.campaigns.contentrewards import PlatformRate
from clipforge.campaigns.scoring import clip_earnings
from clipforge.core.models import (
    Campaign,
    ClipCandidate,
    Post,
    PostStatus,
    RenderedClip,
    SourceAsset,
    Submission,
)
from clipforge.core.settings import Settings


@dataclass
class PostEarnings:
    post_id: int
    campaign_id: int | None
    campaign: str
    platform: str
    url: str | None
    views: int
    likes: int
    engagement_rate: float | None
    min_engagement_ok: bool | None
    gross_usd: float
    net_usd: float
    views_for_min_payout: int | None
    capped: bool
    submitted: bool


def post_campaign(session: Session, post: Post) -> Campaign | None:
    render = session.get(RenderedClip, post.rendered_clip_id)
    clip = session.get(ClipCandidate, render.candidate_id) if render else None
    asset = session.get(SourceAsset, clip.asset_id) if clip else None
    return session.get(Campaign, asset.campaign_id) if asset else None


def _rate(campaign: Campaign | None, platform: str) -> PlatformRate | None:
    if campaign is None:
        return None
    for r in campaign.platform_rates or []:
        if r.get("platform") == platform:
            return PlatformRate.model_validate(r)
    if campaign.cpm_usd:
        return PlatformRate(
            platform=platform, rate_per_1k_usd=campaign.cpm_usd, min_payout_usd=0, max_payout_usd=0
        )
    return None


def estimate(
    session: Session, settings: Settings, min_engagement: float = 0.005
) -> list[PostEarnings]:
    snaps = latest_snapshots(session)
    submitted = {s.post_id for s in session.exec(select(Submission)).all() if s.submitted_at}
    out = []
    posts = session.exec(select(Post).where(Post.status == PostStatus.PUBLISHED)).all()
    for post in posts:
        campaign = post_campaign(session, post)
        platform = post.platform.value if post.platform else "instagram"
        snap = snaps.get(post.id or 0)
        views = snap.views if snap else 0
        likes = snap.likes if snap else 0
        rate = _rate(campaign, platform)
        fee = (
            (campaign.fee_bps / 100)
            if campaign and campaign.fee_bps is not None
            else (settings.analytics.whop_fee_pct)
        )
        est = clip_earnings(views, rate, fee) if rate else None
        uncapped = views * rate.rate_per_1k_usd / 1000 if rate else 0.0
        er = likes / views if views else None
        out.append(
            PostEarnings(
                post_id=post.id or 0,
                campaign_id=campaign.id if campaign else None,
                campaign=(campaign.title or campaign.brand) if campaign else "?",
                platform=platform,
                url=post.published_url,
                views=views,
                likes=likes,
                engagement_rate=round(er, 4) if er is not None else None,
                min_engagement_ok=(er >= min_engagement)
                if er is not None and views >= 200
                else None,
                gross_usd=est.gross_usd if est else 0.0,
                net_usd=est.net_usd if est else 0.0,
                views_for_min_payout=est.views_for_min_payout if est else None,
                capped=bool(rate and rate.max_payout_usd and uncapped > rate.max_payout_usd),
                submitted=(post.id in submitted),
            )
        )
    return out


def totals(rows: list[PostEarnings]) -> dict[str, Any]:
    by_campaign: dict[str, dict[str, float]] = {}
    for r in rows:
        c = by_campaign.setdefault(r.campaign, {"posts": 0, "views": 0, "net_usd": 0.0})
        c["posts"] += 1
        c["views"] += r.views
        c["net_usd"] = round(c["net_usd"] + r.net_usd, 2)
    return {
        "posts": len(rows),
        "views": sum(r.views for r in rows),
        "net_usd": round(sum(r.net_usd for r in rows), 2),
        "by_campaign": by_campaign,
    }
