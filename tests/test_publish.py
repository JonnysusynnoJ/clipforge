from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from clipforge.campaigns.rules import ParsedRules
from clipforge.compliance.check import PostMetadata
from clipforge.core.compliance_report import ComplianceReport
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
from clipforge.core.state import transition
from clipforge.publish import service
from clipforge.publish.metricool import TOOL, build_request
from clipforge.publish.scheduler import next_slot

VIENNA = ZoneInfo("Europe/Vienna")
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)  # 09:00 Vienna
SOURCE = "https://share.example.com/folder"
GOOD_CAPTION = "Great moment @brand\n#Ad\n#Clip"


# --------------------------------------------------------------------------- scheduler


def slot(taken: list[datetime], *, limit: int = 3, gap: int = 120, now: datetime = NOW) -> datetime:
    return next_slot(
        now=now,
        taken=taken,
        daily_limit=limit,
        min_gap=timedelta(minutes=gap),
        tz="Europe/Vienna",
        window_start=time(9),
        window_end=time(23),
        lead=timedelta(minutes=30),
    )


def test_first_slot_respects_lead_and_window() -> None:
    s = slot([])
    assert s.tzinfo is not None and s.astimezone(VIENNA).hour == 9
    assert s >= NOW + timedelta(minutes=30)
    late = slot([], now=datetime(2026, 11, 6, 22, 50, tzinfo=UTC))  # 23:50 Vienna
    assert late.astimezone(VIENNA).date() == datetime(2026, 11, 7).date()
    assert late.astimezone(VIENNA).hour == 9


def test_min_gap_between_posts() -> None:
    first = slot([])
    second = slot([first])
    assert second - first >= timedelta(minutes=120)


def test_daily_limit_moves_to_next_day() -> None:
    taken: list[datetime] = []
    for _ in range(3):
        taken.append(slot(taken, limit=3))
    days = [t.astimezone(VIENNA).date() for t in taken]
    assert len(set(days)) == 1
    fourth = slot(taken, limit=3)
    assert fourth.astimezone(VIENNA).date() > days[0]


def test_limit_one_per_day_over_a_week() -> None:
    taken: list[datetime] = []
    for _ in range(7):
        taken.append(slot(taken, limit=1))
    days = [t.astimezone(VIENNA).date() for t in taken]
    assert len(set(days)) == 7  # exactly one per day
    assert all(b - a >= timedelta(minutes=120) for a, b in pairwise(taken))


def test_preferred_hours_first() -> None:
    s = next_slot(
        now=NOW,
        taken=[],
        daily_limit=3,
        min_gap=timedelta(hours=2),
        tz="Europe/Vienna",
        window_start=time(9),
        window_end=time(23),
        preferred_hours=[18, 20],
    )
    assert s.astimezone(VIENNA).hour == 18


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError):
        slot([], now=datetime(2026, 11, 6, 8, 0))


# --------------------------------------------------------------------------- metricool payload


@pytest.mark.parametrize("platform", list(Platform))
def test_metricool_request(platform: Platform) -> None:
    when = datetime(2026, 11, 7, 18, 0, tzinfo=VIENNA)
    req = build_request(
        blog_id="7921004",
        platform=platform,
        when=when,
        timezone="Europe/Vienna",
        caption=GOOD_CAPTION,
        media_url="https://pub.example.r2.dev/c.mp4",
        draft=True,
    )
    assert req["tool"] == TOOL
    args = req["args"]
    assert args["blogId"] == "7921004" and args["date"] == "2026-11-07T18:00:00+01:00"
    info = json.loads(args["info"])
    assert info["draft"] is True and info["text"] == GOOD_CAPTION
    assert info["providers"] == [{"network": platform.value}]
    assert info["publicationDate"] == {
        "dateTime": "2026-11-07T18:00:00",
        "timezone": "Europe/Vienna",
    }
    assert info["media"] == ["https://pub.example.r2.dev/c.mp4"]
    if platform is Platform.TIKTOK:
        assert info["tiktokData"]["commercialContentThirdParty"] is True
    if platform is Platform.INSTAGRAM:
        assert info["instagramData"]["type"] == "REEL"
    if platform is Platform.YOUTUBE:
        assert info["youtubeData"]["type"] == "short" and info["youtubeData"]["title"]


# --------------------------------------------------------------------------- service


class FakeHost:
    def __init__(self, reachable: bool = True) -> None:
        self.uploaded: list[str] = []
        self.reachable = reachable

    def upload(self, path: Path, key: str) -> str:
        assert path.exists()
        self.uploaded.append(key)
        return f"https://pub.example.r2.dev/{key}"

    def verify(self, url: str) -> bool:
        return self.reachable

    def delete(self, key: str) -> None:
        pass


RULES = ParsedRules(
    allowed_platforms=[Platform.INSTAGRAM, Platform.TIKTOK],
    allowed_sources=[SOURCE],
    min_seconds=5,
    max_seconds=60,
    required_mentions=["@brand"],
    disclosure_options=["#Ad"],
    disclosure_own_line_first=True,
    max_extra_hashtags=3,
)


@pytest.fixture
def pub_settings(settings: Settings) -> Settings:
    settings.publishing.enabled = True
    settings.publishing.mode = "draft"
    settings.rate_limits.max_posts_per_account_per_day = 1
    return settings


def make_campaign(session: Session) -> Campaign:
    c = Campaign(
        whop_url="https://contentrewards.com/c/campaigns/x",
        brand="B",
        parsed_rules=RULES.model_dump(mode="json"),
        status=CampaignStatus.ACTIVE,
        raw_rules_text="brief",
    )
    session.add(c)
    session.flush()
    return c


def approved_clip(
    session: Session, settings: Settings, campaign: Campaign, tmp: Path, caption: str = GOOD_CAPTION
) -> ClipCandidate:
    asset = SourceAsset(campaign_id=campaign.id or 0, origin_url=SOURCE)
    session.add(asset)
    session.flush()
    clip = ClipCandidate(asset_id=asset.id or 0, start=0, end=20, llm_score=8)
    session.add(clip)
    session.flush()
    video = tmp / f"clip_{clip.id}.mp4"
    video.write_bytes(b"fake mp4 " + str(clip.id).encode())
    render = RenderedClip(
        candidate_id=clip.id or 0,
        path=str(video),
        platform_profile="universal",
        compliance_report=ComplianceReport(checks_run=["x"]).model_dump(mode="json"),
        agent_review={"decision": "approve", "score": 8},
        post_metadata=PostMetadata(caption=caption, platforms=[Platform.INSTAGRAM]).model_dump(
            mode="json"
        ),
    )
    session.add(render)
    session.flush()
    transition(session, clip, ClipStatus.RENDERED, actor=Actor.SYSTEM)
    transition(session, clip, ClipStatus.PENDING_REVIEW, actor=Actor.SYSTEM)
    transition(session, clip, ClipStatus.APPROVED, actor=Actor.AGENT)
    return clip


def add_account(session: Session, **over: object) -> SocialAccount:
    data: dict[str, object] = dict(
        platform=Platform.INSTAGRAM, handle="me", whop_linked=True, metricool_blog_id="7921004"
    )
    data.update(over)
    acc = SocialAccount(**data)  # type: ignore[arg-type]
    session.add(acc)
    session.flush()
    return acc


def test_kill_switch_blocks_everything(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    pub_settings.publishing.enabled = False
    campaign = make_campaign(session)
    add_account(session)
    approved_clip(session, pub_settings, campaign, tmp_path)
    host = FakeHost()
    out = service.plan_posts(session, pub_settings, now=NOW, host=host)
    assert not out["planned"] and "kill switch" in out["blocked"][0]["why"]
    assert host.uploaded == []


def test_unlinked_or_private_accounts_are_blocked(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    campaign = make_campaign(session)
    add_account(session, whop_linked=False, handle="a")
    add_account(session, is_public=False, handle="b")
    add_account(session, platform=Platform.YOUTUBE, handle="c")  # not allowed by campaign
    approved_clip(session, pub_settings, campaign, tmp_path)
    out = service.plan_posts(session, pub_settings, now=NOW, host=FakeHost())
    assert not out["planned"] and "no eligible account" in out["blocked"][0]["why"]


def test_compliance_and_disclosure_rechecked_before_publish(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    campaign = make_campaign(session)
    add_account(session)
    clip = approved_clip(session, pub_settings, campaign, tmp_path)
    # caption edited after approval: disclosure removed
    render = service.latest_render(session, clip)
    assert render is not None
    render.post_metadata = {**render.post_metadata, "caption": "Great moment @brand"}
    host = FakeHost()
    out = service.plan_posts(session, pub_settings, now=NOW, host=host)
    assert not out["planned"] and "compliance" in out["blocked"][0]["why"]
    assert host.uploaded == []


def test_unreachable_media_blocks(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    campaign = make_campaign(session)
    add_account(session)
    approved_clip(session, pub_settings, campaign, tmp_path)
    out = service.plan_posts(session, pub_settings, now=NOW, host=FakeHost(reachable=False))
    assert not out["planned"] and "not publicly reachable" in out["blocked"][0]["why"]


def test_full_flow_rate_limited_one_post_per_clip(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    campaign = make_campaign(session)
    acc = add_account(session)
    clips = [approved_clip(session, pub_settings, campaign, tmp_path) for _ in range(3)]
    host = FakeHost()
    out = service.plan_posts(session, pub_settings, now=NOW, host=host)
    assert len(out["planned"]) == 3 and len(host.uploaded) == 3
    days = {datetime.fromisoformat(p["at"]).astimezone(VIENNA).date() for p in out["planned"]}
    assert len(days) == 3  # 1 post per account per day
    assert all(p["draft"] for p in out["planned"])

    # nothing is planned twice
    again = service.plan_posts(session, pub_settings, now=NOW, host=host)
    assert again["planned"] == [] and len(host.uploaded) == 3

    q = service.queue(session)
    assert len(q) == 3 and json.loads(q[0]["args"]["info"])["draft"] is True

    post_id = out["planned"][0]["post_id"]
    service.record_result(session, post_id, ok=True, metricool_post_id="m1", planner_url="u")
    clip0 = next(c for c in clips if c.id == out["planned"][0]["clip_id"])
    assert clip0.status is ClipStatus.SCHEDULED and len(service.queue(session)) == 2

    service.mark_published(session, post_id, "https://instagram.com/reel/x")
    assert clip0.status is ClipStatus.PUBLISHED
    with pytest.raises(ValueError):
        service.mark_published(session, post_id, "again")

    failed_id = out["planned"][1]["post_id"]
    service.record_result(session, failed_id, ok=False, error="media rejected")
    post = session.get(Post, failed_id)
    assert post is not None and post.status is PostStatus.FAILED and post.error == "media rejected"
    assert acc.id is not None


def test_cancel_returns_clip_to_approved(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    campaign = make_campaign(session)
    add_account(session)
    clip = approved_clip(session, pub_settings, campaign, tmp_path)
    out = service.plan_posts(session, pub_settings, now=NOW, host=FakeHost())
    pid = out["planned"][0]["post_id"]
    service.record_result(session, pid, ok=True)
    service.cancel(session, pid, "owner request")
    assert clip.status is ClipStatus.APPROVED


def test_manual_export(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    campaign = make_campaign(session)
    add_account(session)
    approved_clip(session, pub_settings, campaign, tmp_path)
    out = service.plan_posts(session, pub_settings, now=NOW, host=FakeHost())
    target = service.export_manual(session, pub_settings, out["planned"][0]["post_id"])
    assert (target / "video.mp4").exists()
    assert (target / "caption.txt").read_text() == GOOD_CAPTION
