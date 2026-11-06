"""Campaign scoring: expected earnings per clip, budget runway and risk flags."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from clipforge.campaigns.contentrewards import DiscoveredCampaign, PlatformRate


class PlatformEstimate(BaseModel):
    platform: str
    gross_usd: float
    net_usd: float
    views_for_min_payout: int | None


class CampaignScore(BaseModel):
    score: float
    expected_net_per_clip_usd: float
    best_platform: str | None
    estimates: list[PlatformEstimate] = Field(default_factory=list)
    burn_per_day_usd: float | None = None
    runway_days: float | None = None
    flags: list[str] = Field(default_factory=list)
    blocked: bool = False


def clip_earnings(views: int, rate: PlatformRate, fee_pct: float) -> PlatformEstimate:
    """Views x CPM, zero below the minimum payout, capped at the maximum, minus fee."""
    gross = views * rate.rate_per_1k_usd / 1000
    if gross < rate.min_payout_usd:
        gross = 0.0
    if rate.max_payout_usd:
        gross = min(gross, rate.max_payout_usd)
    needed = (
        int(rate.min_payout_usd * 1000 / rate.rate_per_1k_usd + 0.999)
        if rate.rate_per_1k_usd
        else None
    )
    return PlatformEstimate(
        platform=rate.platform,
        gross_usd=round(gross, 2),
        net_usd=round(gross * (1 - fee_pct / 100), 2),
        views_for_min_payout=needed,
    )


def is_blocked(c: DiscoveredCampaign, categories: list[str], keywords: list[str]) -> str | None:
    if c.category and c.category.lower() in {x.lower() for x in categories}:
        return f"blocked category: {c.category}"
    text = f"{c.title} {c.brand} {c.description}".lower()
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw.lower())}", text):
            return f"blocked keyword: {kw}"
    return None


_LANGUAGE_MARKERS = {
    "fr": ["français", "francais", "french", "en français"],
    "es": ["español", "espanol", "spanish", "latam"],
    "de": ["deutsch", "german", "auf deutsch"],
    "ar": ["arabic", "عربي"],
    "tr": ["turkish", "türkçe"],
    "pt": ["português", "portugues", "brasil", "brazil"],
    "it": ["italiano"],
}


def detect_language(text: str) -> str | None:
    """Very small heuristic: explicit language markers or non-Latin script in the title."""
    lower = text.lower()
    for lang, markers in _LANGUAGE_MARKERS.items():
        if any(m in lower for m in markers):
            return lang
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]", text):
        return "cjk"
    return None


def score_campaign(
    c: DiscoveredCampaign,
    *,
    my_platforms: set[str],
    median_views: int,
    default_fee_pct: float,
    blocked_categories: list[str],
    blocked_keywords: list[str],
    min_budget_left_usd: float,
    risk_keywords: list[str] | None = None,
    content_language: str = "en",
    now: datetime | None = None,
) -> CampaignScore:
    now = now or datetime.now(UTC)
    flags: list[str] = []
    block_reason = is_blocked(c, blocked_categories, blocked_keywords)
    if block_reason:
        flags.append(block_reason)
    text = f"{c.title} {c.description}".lower()
    risky = [kw for kw in risk_keywords or [] if kw.lower() in text]
    if risky:
        flags.append(f"sensitive topic: {', '.join(risky)}")
    lang = detect_language(c.title)
    lang_mismatch = lang is not None and lang != content_language
    if lang_mismatch:
        flags.append(f"content language looks like '{lang}', not '{content_language}'")

    fee_pct = c.fee_bps / 100 if c.fee_bps is not None else default_fee_pct
    rates = c.platform_rates or [
        PlatformRate(platform=p, rate_per_1k_usd=c.max_rate_per_1k_usd or 0,
                     min_payout_usd=0, max_payout_usd=0)
        for p in c.platforms
    ]  # fmt: skip
    usable = [r for r in rates if r.platform in my_platforms]
    if not usable:
        flags.append("no matching account platform")
    estimates = [clip_earnings(median_views, r, fee_pct) for r in usable]
    best = max(estimates, key=lambda e: e.net_usd, default=None)
    if best and best.net_usd == 0:
        flags.append(f"median views below min payout (needs {best.views_for_min_payout} views)")

    burn = runway = None
    if c.budget_spent_usd and c.launched_at:
        days = max((now - c.launched_at).total_seconds() / 86400, 0.5)
        burn = c.budget_spent_usd / days
        if c.budget_left_usd is not None and burn > 0:
            runway = c.budget_left_usd / burn
            if runway < 3:
                flags.append(f"budget runs out in ~{runway:.1f} days")
    if c.budget_left_usd is not None and c.budget_left_usd < min_budget_left_usd:
        flags.append(f"low budget left (${c.budget_left_usd:.0f})")
    if c.requires_application:
        flags.append("requires application")
    if c.type != "cpm":
        flags.append(f"payout type {c.type} not estimated")
    if c.status and c.status != "active":
        flags.append(f"status {c.status}")
    if not c.detail_loaded:
        flags.append("detail not loaded")

    blocked = bool(block_reason) or not usable or (c.status not in (None, "active"))
    expected = best.net_usd if best else 0.0
    # Rank by expected money; when everyone is below the payout threshold (new accounts),
    # prefer campaigns where the threshold is easiest to reach and the rate is highest.
    reach = 0.0
    if best and best.views_for_min_payout:
        reach = min(1.0, median_views / best.views_for_min_payout)
    best_rate = max((r.rate_per_1k_usd for r in usable), default=0.0)
    runway_factor = 1.0 if runway is None else min(1.0, runway / 7)
    score = (expected + reach * best_rate) * runway_factor
    if c.requires_application:
        score *= 0.5
    if lang_mismatch:
        score *= 0.2
    if risky:
        score *= 0.7
    if c.budget_left_usd is not None and c.budget_left_usd < min_budget_left_usd:
        score *= 0.3
    if blocked:
        score = 0.0
    return CampaignScore(
        score=round(score, 4),
        expected_net_per_clip_usd=expected,
        best_platform=best.platform if best else None,
        estimates=estimates,
        burn_per_day_usd=round(burn, 2) if burn else None,
        runway_days=round(runway, 1) if runway else None,
        flags=flags,
        blocked=blocked,
    )
