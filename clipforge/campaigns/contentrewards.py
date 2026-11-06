"""Public campaign discovery on contentrewards.com (Whop Content Rewards).

Only public, server-rendered pages are read (`/c/discover/...`, `/c/campaigns/<id>`).
`robots.txt` disallows `/api/`, so we never call it; nothing behind a login is accessed.
The pages embed their data in the Next.js flight payload (`self.__next_f.push`).
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

BASE = "https://contentrewards.com"
USER_AGENT = "clipforge/0.1 (campaign discovery; respects robots.txt)"
_MIN_INTERVAL_S = 1.5
_last_request = 0.0


class PlatformRate(BaseModel):
    platform: str
    rate_per_1k_usd: float
    min_payout_usd: float
    max_payout_usd: float


class DiscoveredCampaign(BaseModel):
    external_id: str
    url: str
    title: str
    brand: str
    category: str | None = None
    description: str = ""
    platforms: list[str] = Field(default_factory=list)
    type: str = "cpm"
    budget_total_usd: float | None = None
    budget_spent_usd: float | None = None
    budget_left_usd: float | None = None
    rate_label: str | None = None
    max_rate_per_1k_usd: float | None = None
    requires_application: bool = False
    launched_at: datetime | None = None
    creator_count: int | None = None
    submission_count: int | None = None
    # detail page only
    platform_rates: list[PlatformRate] = Field(default_factory=list)
    fee_bps: int | None = None
    status: str | None = None
    submit_url: str | None = None
    total_views: int | None = None
    detail_loaded: bool = False


def _get(url: str, timeout: float = 60) -> str:
    global _last_request
    if "/api/" in url:
        raise ValueError("refusing to call /api/ (disallowed by robots.txt)")
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    _last_request = time.monotonic()
    return body.decode("utf-8", errors="replace")


def flight_payload(page: str) -> str:
    """Concatenate the Next.js flight chunks embedded in a server-rendered page."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.S)
    return "".join(json.loads(f'"{c}"') for c in chunks)


def _objects(payload: str, start_pattern: str) -> list[dict[str, Any]]:
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(start_pattern, payload):
        try:
            obj, _ = dec.raw_decode(payload[m.start() :])
        except json.JSONDecodeError:
            continue
        out.append(obj)
    return out


def _money(label: str | None) -> float | None:
    if not label:
        return None
    m = re.search(r"([\d.]+)", label.replace(",", ""))
    return float(m.group(1)) if m else None


def parse_discover(page: str) -> list[DiscoveredCampaign]:
    seen: dict[str, DiscoveredCampaign] = {}
    for card in _objects(flight_payload(page), r'\{"availableBudgetRaw"'):
        cid = card.get("id")
        if not cid or cid in seen:
            continue
        launched = card.get("launchedAtMs")
        seen[cid] = DiscoveredCampaign(
            external_id=cid,
            url=f"{BASE}/c/campaigns/{cid}",
            title=str(card.get("title", "")).replace("$$", "$"),
            brand=card.get("brand") or "",
            category=card.get("category"),
            description=card.get("description") or "",
            platforms=card.get("platforms") or [],
            type=card.get("type") or "cpm",
            budget_total_usd=card.get("budgetTotalRaw"),
            budget_spent_usd=card.get("budgetSpentRaw"),
            budget_left_usd=card.get("availableBudgetRaw"),
            rate_label=str(card.get("ratePer1kLabel", "")).replace("$$", "$") or None,
            max_rate_per_1k_usd=_money(card.get("ratePer1kLabel")),
            requires_application=bool(card.get("requiresApplication")),
            launched_at=datetime.fromtimestamp(launched / 1000, UTC) if launched else None,
            creator_count=card.get("creatorCountRaw"),
            submission_count=card.get("submissionCountRaw"),
        )
    return list(seen.values())


def parse_detail(page: str, campaign: DiscoveredCampaign) -> DiscoveredCampaign:
    payload = flight_payload(page)
    detail = next(
        (
            o
            for o in _objects(payload, r'\{"banner"')
            if o.get("id") == campaign.external_id and "configuration" in o
        ),
        None,
    )
    updated = campaign.model_copy(deep=True)
    if detail is None:
        return updated
    cfg = detail.get("configuration", {})
    updated.platform_rates = [
        PlatformRate(
            platform=p["platform"],
            rate_per_1k_usd=p["rate"] / 100,
            min_payout_usd=p.get("minPayout", 0) / 100,
            max_payout_usd=p.get("maxPayout", 0) / 100,
        )
        for p in cfg.get("payoutModel", [])
        if "rate" in p
    ]
    updated.fee_bps = cfg.get("fee")
    updated.status = detail.get("status")
    if "requiresApplication" in detail:
        updated.requires_application = bool(detail["requiresApplication"])
    if cfg.get("budget") is not None:
        updated.budget_total_usd = cfg["budget"] / 100
    metrics = detail.get("metrics", {})
    if metrics.get("budgetSpentCents") is not None and updated.budget_total_usd is not None:
        updated.budget_spent_usd = metrics["budgetSpentCents"] / 100
        updated.budget_left_usd = updated.budget_total_usd - updated.budget_spent_usd
    updated.total_views = metrics.get("totalViews")
    m = re.search(
        r"https://whop\.com/experiences/exp_\w+/campaigns/" + re.escape(campaign.external_id),
        payload.replace("%2F", "/").replace("%3A", ":"),
    )
    updated.submit_url = m.group(0) if m else None
    updated.detail_loaded = True
    return updated


def discover(section: str = "clipping") -> list[DiscoveredCampaign]:
    return parse_discover(_get(f"{BASE}/c/discover/{section}"))


def load_detail(campaign: DiscoveredCampaign) -> DiscoveredCampaign:
    return parse_detail(_get(campaign.url), campaign)


def detail_by_id(external_id: str) -> DiscoveredCampaign:
    """Load a single campaign directly from its public page (manual add by URL)."""
    stub = DiscoveredCampaign(
        external_id=external_id,
        url=f"{BASE}/c/campaigns/{external_id}",
        title="",
        brand="",
    )
    page = _get(stub.url)
    payload = flight_payload(page)
    detail = next((o for o in _objects(payload, r'\{"banner"') if o.get("id") == external_id), None)
    if detail is not None:
        stub.title = detail.get("name", "")
        stub.brand = (detail.get("organization") or {}).get("name", "")
        stub.description = detail.get("description") or ""
        cats = (detail.get("configuration", {}).get("content", {}) or {}).get("contentCategories")
        stub.category = cats[0]["name"] if cats else None
        stub.platforms = [p["platform"] for p in detail["configuration"].get("payoutModel", [])]
        stub.type = detail.get("type", "cpm")
        if detail.get("launchDate"):
            stub.launched_at = datetime.fromisoformat(detail["launchDate"].replace("Z", "+00:00"))
    return parse_detail(page, stub)


def external_id_from_url(url: str) -> str:
    m = re.search(r"/campaigns/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError(f"not a campaign URL: {url}")
    return m.group(1)
