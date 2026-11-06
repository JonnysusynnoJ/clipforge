"""Campaign discovery, scoring, brief handling and selection (DB side)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from clipforge.campaigns import contentrewards as cr
from clipforge.campaigns.brief import fetch_brief, rules_request, validate_rules
from clipforge.campaigns.rules import ParsedRules
from clipforge.campaigns.scoring import CampaignScore, score_campaign
from clipforge.core.logging import get_logger
from clipforge.core.models import Campaign, CampaignStatus, SocialAccount
from clipforge.core.settings import Settings

log = get_logger(__name__)


def my_platforms(session: Session) -> set[str]:
    rows = session.exec(select(SocialAccount).where(SocialAccount.active == True)).all()  # noqa: E712
    return {a.platform.value for a in rows}


def _score(settings: Settings, c: cr.DiscoveredCampaign, platforms: set[str]) -> CampaignScore:
    cfg = settings.campaigns
    return score_campaign(
        c,
        my_platforms=platforms,
        median_views=settings.analytics.default_median_views,
        default_fee_pct=settings.analytics.whop_fee_pct,
        blocked_categories=cfg.blocked_categories,
        blocked_keywords=cfg.blocked_keywords,
        min_budget_left_usd=cfg.min_budget_left_usd,
        risk_keywords=cfg.risk_keywords,
        content_language=cfg.content_language,
    )


def upsert_discovered(session: Session, d: cr.DiscoveredCampaign, score: CampaignScore) -> Campaign:
    row = session.exec(select(Campaign).where(Campaign.external_id == d.external_id)).first()
    row = row or session.exec(select(Campaign).where(Campaign.whop_url == d.url)).first()
    row = row or Campaign(whop_url=d.url, brand=d.brand)
    row.external_id = d.external_id
    row.whop_url = d.url
    row.title = d.title
    row.brand = d.brand
    row.category = d.category
    row.description = d.description
    row.payout_type = d.type
    row.platforms = d.platforms
    row.budget_total = d.budget_total_usd
    row.budget_left = d.budget_left_usd
    row.requires_application = d.requires_application
    row.launched_at = d.launched_at
    if d.detail_loaded:
        row.platform_rates = [r.model_dump() for r in d.platform_rates]
        row.fee_bps = d.fee_bps
        row.submit_url = d.submit_url or row.submit_url
        rates = [r.rate_per_1k_usd for r in d.platform_rates]
        row.cpm_usd = max(rates) if rates else row.cpm_usd
    elif d.max_rate_per_1k_usd is not None:
        row.cpm_usd = d.max_rate_per_1k_usd
    row.score = score.score
    row.score_report = score.model_dump(mode="json")
    if score.blocked and row.status is CampaignStatus.DISCOVERED:
        row.status = CampaignStatus.IGNORED
    if (d.status and d.status != "active") or (
        d.budget_left_usd is not None and d.budget_left_usd <= 0
    ):
        row.status = CampaignStatus.EXHAUSTED
    session.add(row)
    session.flush()
    return row


def discover_and_score(
    session: Session,
    settings: Settings,
    *,
    fetch_section: Callable[[str], list[cr.DiscoveredCampaign]] = cr.discover,
    fetch_detail: Callable[[cr.DiscoveredCampaign], cr.DiscoveredCampaign] = cr.load_detail,
) -> list[Campaign]:
    platforms = my_platforms(session)
    found: dict[str, cr.DiscoveredCampaign] = {}
    for section in settings.campaigns.discover_sections:
        for d in fetch_section(section):
            found.setdefault(d.external_id, d)
    pre = sorted(found.values(), key=lambda d: _score(settings, d, platforms).score, reverse=True)
    top = {d.external_id for d in pre[: settings.campaigns.detail_top_n]}
    tracked = session.exec(
        select(Campaign.external_id).where(
            Campaign.status.in_([CampaignStatus.ACTIVE, CampaignStatus.PAUSED])  # type: ignore[attr-defined]
        )
    ).all()
    top |= {x for x in tracked if x}
    rows = []
    for d in pre:
        if d.external_id in top:
            try:
                d = fetch_detail(d)
            except Exception as exc:  # one broken page must not stop discovery
                log.warning("detail_failed", campaign=d.external_id, error=str(exc))
        rows.append(upsert_discovered(session, d, _score(settings, d, platforms)))
    return rows


def add_by_url(session: Session, settings: Settings, url: str) -> Campaign:
    d = cr.detail_by_id(cr.external_id_from_url(url))
    return upsert_discovered(session, d, _score(settings, d, my_platforms(session)))


def campaign_dir(settings: Settings, campaign: Campaign) -> Path:
    d = settings.paths.data_dir / "campaigns" / str(campaign.id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_brief(
    session: Session,
    settings: Settings,
    campaign: Campaign,
    *,
    url: str | None = None,
    text: str | None = None,
) -> Path:
    """Store the brief text (fetched from a public link or given) and write rules_request.json."""
    links: list[str] = []
    if url:
        text, links = fetch_brief(url)
        campaign.brief_url = url
    if not text:
        raise ValueError("provide a brief URL or text")
    campaign.raw_rules_text = text
    session.add(campaign)
    req = rules_request(
        campaign_id=campaign.id or 0,
        title=campaign.title or campaign.brand,
        platforms=campaign.platforms,
        text=text,
        links=links,
    )
    path = campaign_dir(settings, campaign) / "rules_request.json"
    path.write_text(json.dumps(req, indent=2))
    return path


def apply_rules(
    session: Session, campaign: Campaign, raw_json: str
) -> tuple[str | None, list[str]]:
    """Validate the agent's rules and store them.

    Returns (error, warnings). An error means nothing was stored and the agent must fix
    and retry. Warnings are stored rules that leave the campaign not ready (e.g. no
    authorized material source yet).
    """
    rules, error = validate_rules(raw_json)
    if rules is None:
        return error, []
    problems = []
    if not campaign.raw_rules_text:
        problems.append("campaign has no brief text; run campaign-brief first")
    if campaign.platforms:
        extra = [p.value for p in rules.allowed_platforms if p.value not in campaign.platforms]
        if extra:
            problems.append(f"allowed_platforms not offered by the campaign: {extra}")
    if not rules.allowed_platforms:
        problems.append("allowed_platforms is empty")
    if problems:
        return "\n".join(problems), []
    warnings = []
    if not rules.allowed_sources:
        warnings.append("allowed_sources is empty: campaign stays not ready, nothing is downloaded")
    campaign.parsed_rules = rules.model_dump(mode="json")
    session.add(campaign)
    return None, warnings


def is_ready(campaign: Campaign) -> bool:
    if not campaign.parsed_rules:
        return False
    rules = ParsedRules.model_validate(campaign.parsed_rules)
    return bool(rules.allowed_sources) and bool(campaign.raw_rules_text)


def select_active(session: Session, settings: Settings) -> dict[str, list[str]]:
    """Activate the best ready campaigns (max `campaigns.max_active`); pause the rest."""
    rows = session.exec(
        select(Campaign).where(
            Campaign.status.in_(  # type: ignore[attr-defined]
                [CampaignStatus.DISCOVERED, CampaignStatus.ACTIVE, CampaignStatus.PAUSED]
            )
        )
    ).all()
    ready = [c for c in rows if is_ready(c) and not (c.score_report or {}).get("blocked")]
    ready.sort(key=lambda c: c.score or 0, reverse=True)
    chosen = ready[: settings.campaigns.max_active]
    result: dict[str, list[str]] = {"active": [], "paused": [], "needs_brief": []}
    for c in sorted(rows, key=lambda c: c.score or 0, reverse=True):
        label = c.title or c.brand
        if c in chosen:
            c.status = CampaignStatus.ACTIVE
            result["active"].append(label)
        elif c.status is CampaignStatus.ACTIVE:
            c.status = CampaignStatus.PAUSED
            result["paused"].append(label)
        elif not is_ready(c) and c.status is CampaignStatus.DISCOVERED and (c.score or 0) > 0:
            result["needs_brief"].append(label)
        session.add(c)
    result["needs_brief"] = result["needs_brief"][:10]
    return result


def summary_rows(session: Session, limit: int = 30) -> list[dict[str, Any]]:
    rows = session.exec(select(Campaign)).all()
    rows = sorted(rows, key=lambda c: c.score or 0, reverse=True)[:limit]
    out = []
    for c in rows:
        rep = c.score_report or {}
        out.append(
            {
                "id": c.id,
                "title": (c.title or c.brand)[:48],
                "status": c.status.value,
                "score": c.score,
                "cpm": c.cpm_usd,
                "budget_left": c.budget_left,
                "runway_days": rep.get("runway_days"),
                "ev_clip": rep.get("expected_net_per_clip_usd"),
                "ready": is_ready(c),
                "flags": rep.get("flags", []),
                "launched": c.launched_at.astimezone(UTC).date().isoformat()
                if c.launched_at
                else None,
            }
        )
    return out
