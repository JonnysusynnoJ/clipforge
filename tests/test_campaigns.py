from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from clipforge.campaigns import contentrewards as cr
from clipforge.campaigns.brief import html_to_text, rules_request, source_link_candidates
from clipforge.campaigns.scoring import clip_earnings, detect_language, score_campaign
from clipforge.campaigns.service import (
    apply_rules,
    discover_and_score,
    is_ready,
    select_active,
    set_brief,
)
from clipforge.core.models import Campaign, CampaignStatus, Platform, SocialAccount
from clipforge.core.settings import Settings

NOW = datetime(2026, 11, 6, tzinfo=UTC)
CID = "11111111-2222-3333-4444-555555555555"


def page(payload: str) -> str:
    """Wrap a payload the way Next.js embeds flight data in server-rendered HTML."""
    half = len(payload) // 2
    return "".join(
        f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
        for part in (payload[:half], payload[half:])
    )


def card(cid: str = CID, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "availableBudgetRaw": 4000.0,
        "brand": "Studio",
        "budgetSpentRaw": 6000.0,
        "budgetTotalRaw": 10000,
        "category": "Entertainment",
        "creatorCountRaw": 10,
        "description": "Clip the trailer.",
        "id": cid,
        "launchedAtMs": int((NOW - timedelta(days=6)).timestamp() * 1000),
        "platforms": ["instagram", "tiktok"],
        "requiresApplication": False,
        "submissionCountRaw": 50,
        "title": "$$10,000 Trailer Clipping",
        "ratePer1kLabel": "$$2",
        "type": "cpm",
    }
    base.update(over)
    return base


def detail(cid: str = CID) -> dict[str, object]:
    return {
        "banner": "x",
        "id": cid,
        "name": "Trailer Clipping",
        "status": "active",
        "organization": {"name": "Studio"},
        "metrics": {"budgetSpentCents": 600000, "totalViews": 123},
        "configuration": {
            "fee": 800,
            "budget": 1000000,
            "content": {"contentCategories": [{"id": "e", "name": "Entertainment"}]},
            "payoutModel": [
                {"platform": "instagram", "rate": 150, "minPayout": 400, "maxPayout": 50000},
                {"platform": "tiktok", "rate": 200, "minPayout": 0, "maxPayout": 50000},
            ],
        },
        "type": "cpm",
        "launchDate": "2026-10-31T00:00:00.000Z",
    }


def detail_page(cid: str = CID) -> str:
    whop = (
        "https://whop.com/checkout/plan_x?redirect_url=https%3A%2F%2Fwhop.com%2Fexperiences"
        f"%2Fexp_abc123%2Fcampaigns%2F{cid}"
    )
    payload = json.dumps({"queries": [{"state": {"data": detail(cid)}}], "href": whop})
    return page(payload)


# --------------------------------------------------------------------------- parsing


def test_parse_discover_cards() -> None:
    payload = json.dumps([card(), card(), card("22222222-2222-3333-4444-555555555555")])
    found = cr.parse_discover(page(payload))
    assert [c.external_id for c in found] == [CID, "22222222-2222-3333-4444-555555555555"]
    c = found[0]
    assert c.title == "$10,000 Trailer Clipping" and c.max_rate_per_1k_usd == 2.0
    assert c.budget_left_usd == 4000 and c.url.endswith(CID) and not c.detail_loaded


def test_parse_detail_rates_fee_and_submit_url() -> None:
    [c] = cr.parse_discover(page(json.dumps([card()])))
    d = cr.parse_detail(detail_page(), c)
    assert d.detail_loaded and d.fee_bps == 800 and d.status == "active"
    ig = next(r for r in d.platform_rates if r.platform == "instagram")
    assert (ig.rate_per_1k_usd, ig.min_payout_usd, ig.max_payout_usd) == (1.5, 4.0, 500.0)
    assert d.budget_left_usd == pytest.approx(4000)
    assert d.submit_url == f"https://whop.com/experiences/exp_abc123/campaigns/{CID}"


def test_api_paths_are_refused() -> None:
    with pytest.raises(ValueError, match="robots"):
        cr._get("https://contentrewards.com/api/campaigns")


def test_external_id_from_url() -> None:
    assert cr.external_id_from_url(f"https://contentrewards.com/c/campaigns/{CID}") == CID
    with pytest.raises(ValueError):
        cr.external_id_from_url("https://contentrewards.com/c/discover")


# --------------------------------------------------------------------------- scoring


def test_clip_earnings_threshold_cap_fee() -> None:
    rate = cr.PlatformRate(platform="instagram", rate_per_1k_usd=1.35, min_payout_usd=4,
                           max_payout_usd=10)  # fmt: skip
    assert clip_earnings(1000, rate, 8).net_usd == 0  # below minimum payout
    assert clip_earnings(1000, rate, 8).views_for_min_payout == 2963
    assert clip_earnings(5000, rate, 0).gross_usd == pytest.approx(6.75)
    assert clip_earnings(100_000, rate, 10).net_usd == pytest.approx(9.0)  # capped, minus fee


def _score(c: cr.DiscoveredCampaign, platforms: set[str] | None = None, views: int = 1000):  # type: ignore[no-untyped-def]
    return score_campaign(
        c,
        my_platforms=platforms or {"instagram"},
        median_views=views,
        default_fee_pct=7,
        blocked_categories=["Gambling"],
        blocked_keywords=["casino", "sinparty"],
        min_budget_left_usd=300,
        risk_keywords=["trading"],
        content_language="en",
        now=NOW,
    )


def _detailed(**over: object) -> cr.DiscoveredCampaign:
    data = card(CID)
    data.update(over)
    [c] = cr.parse_discover(page(json.dumps([data])))
    return cr.parse_detail(detail_page(), c)


def test_score_uses_my_platforms_and_runway() -> None:
    c = _detailed()
    ig_only = _score(c)
    assert ig_only.expected_net_per_clip_usd == 0  # $1.50 < $4 minimum on instagram
    assert any("min payout" in f for f in ig_only.flags)
    tiktok = _score(c, {"tiktok"})
    assert tiktok.expected_net_per_clip_usd == pytest.approx(2 * 0.92)
    assert tiktok.score > ig_only.score
    assert tiktok.runway_days == pytest.approx(4000 / (6000 / 6), rel=0.01)


@pytest.mark.parametrize(
    ("over", "flag"),
    [
        ({"category": "Gambling"}, "blocked category"),
        ({"description": "Clip our CASINO streams"}, "blocked keyword"),
        ({"title": "Post Clips for Sinparty"}, "blocked keyword"),
    ],
)
def test_blocked_campaigns_score_zero(over: dict[str, object], flag: str) -> None:
    s = _score(_detailed(**over), {"tiktok"})
    assert s.blocked and s.score == 0 and any(flag in f for f in s.flags)


def test_soft_flags_reduce_score() -> None:
    base = _score(_detailed(), {"tiktok"}).score
    for over in (
        {"title": "Trailer Clipping (en Français)"},
        {"description": "Clip our trading content"},
        {"requiresApplication": True},
    ):
        s = _score(_detailed(**over), {"tiktok"})
        assert not s.blocked and 0 < s.score < base, over
    assert detect_language("Call of Duty MW4 عربي V2") == "ar"
    assert detect_language("Call of Duty MW4 Season 01") is None


def test_no_matching_platform_blocks() -> None:
    s = _score(_detailed(), {"youtube"})
    assert s.blocked and "no matching account platform" in s.flags


# --------------------------------------------------------------------------- brief + rules


def test_brief_text_and_source_links() -> None:
    html = (
        '<p>Use ONLY footage from <a href="https://www.google.com/url?q=https://app.mediasilo.com'
        '/spotlight/abc&amp;sa=D">the folder</a></p><style>x{}</style><p>Tag @brand</p>'
    )
    text, links = html_to_text(html)
    assert "Tag @brand" in text and "x{}" not in text
    assert source_link_candidates(text, links) == ["https://app.mediasilo.com/spotlight/abc"]
    req = rules_request(campaign_id=1, title="t", platforms=["tiktok"], text=text, links=links)
    assert "allowed_sources" in req["schema"]["properties"]  # type: ignore[index]


def _campaign(session: Session, **over: object) -> Campaign:
    c = Campaign(whop_url=f"https://contentrewards.com/c/campaigns/{over.pop('cid', CID)}",
                 brand="Studio", platforms=["instagram", "tiktok"], **over)  # type: ignore[arg-type]  # fmt: skip
    session.add(c)
    session.flush()
    return c


GOOD_RULES = {
    "allowed_platforms": ["instagram"],
    "allowed_sources": ["https://app.mediasilo.com/spotlight/abc"],
    "disclosure_options": ["#Ad"],
}


def test_apply_rules_error_feedback_and_storage(session: Session, settings: Settings) -> None:
    c = _campaign(session)
    err, _ = apply_rules(session, c, json.dumps(GOOD_RULES))
    assert err and "brief" in err  # no brief text yet
    set_brief(session, settings, c, text="Use the official folder. Tag @brand. #Ad")
    assert (settings.paths.data_dir / f"campaigns/{c.id}/rules_request.json").exists()

    err, _ = apply_rules(session, c, "{not json")
    assert err and "invalid JSON" in err
    err, _ = apply_rules(
        session, c, json.dumps({**GOOD_RULES, "min_seconds": 40, "max_seconds": 10})
    )
    assert err and "min_seconds" in err
    err, _ = apply_rules(session, c, json.dumps({**GOOD_RULES, "allowed_platforms": ["youtube"]}))
    assert err and "not offered" in err
    assert not c.parsed_rules

    err, warnings = apply_rules(session, c, json.dumps({**GOOD_RULES, "allowed_sources": []}))
    assert err is None and warnings and not is_ready(c)
    err, warnings = apply_rules(session, c, json.dumps(GOOD_RULES))
    assert err is None and not warnings and is_ready(c)


def test_select_active_respects_limit_and_readiness(session: Session, settings: Settings) -> None:
    settings.campaigns.max_active = 1
    ready = []
    for i, score in enumerate((5.0, 8.0)):
        c = _campaign(session, cid=f"{i}" * 8 + CID[8:], score=score,
                      raw_rules_text="brief", parsed_rules=GOOD_RULES)  # fmt: skip
        ready.append(c)
    unready = _campaign(session, cid="9" * 8 + CID[8:], score=9.0)
    blocked = _campaign(session, cid="8" * 8 + CID[8:], score=9.5, raw_rules_text="b",
                        parsed_rules=GOOD_RULES, score_report={"blocked": True})  # fmt: skip
    result = select_active(session, settings)
    assert ready[1].status is CampaignStatus.ACTIVE
    assert ready[0].status is CampaignStatus.DISCOVERED
    assert (
        unready.status is CampaignStatus.DISCOVERED and blocked.status is not CampaignStatus.ACTIVE
    )
    assert result["needs_brief"][0] == unready.brand


def test_discover_and_score_with_fake_fetchers(session: Session, settings: Settings) -> None:
    session.add(SocialAccount(platform=Platform.TIKTOK, handle="me"))
    session.flush()
    other = "33333333-2222-3333-4444-555555555555"
    cards = cr.parse_discover(page(json.dumps([card(), card(other, category="Gambling")])))
    rows = discover_and_score(
        session,
        settings,
        fetch_section=lambda _s: cards,
        fetch_detail=lambda c: cr.parse_detail(detail_page(c.external_id), c),
    )
    by_id = {r.external_id: r for r in rows}
    assert by_id[CID].fee_bps == 800 and by_id[CID].submit_url
    assert (by_id[CID].score or 0) > 0
    assert by_id[other].status is CampaignStatus.IGNORED and by_id[other].score == 0
    # re-discovery updates instead of duplicating
    rows2 = discover_and_score(session, settings, fetch_section=lambda _s: cards,
                               fetch_detail=lambda c: c)  # fmt: skip
    assert {r.id for r in rows2} == {r.id for r in rows}
