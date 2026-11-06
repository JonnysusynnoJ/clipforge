from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from clipforge.analytics.earnings import estimate, totals
from clipforge.analytics.metrics import METRIC_SETS, import_metrics, latest_snapshots
from clipforge.analytics.report import weekly
from clipforge.core.models import ClipStatus, Post, PostStatus
from clipforge.core.settings import Settings
from clipforge.publish import service
from clipforge.submit import service as submit
from tests.test_publish import NOW, FakeHost, add_account, approved_clip, make_campaign

IG = METRIC_SETS["instagram"]
REQ = list(IG.values())


@pytest.fixture
def pub_settings(settings: Settings) -> Settings:
    settings.publishing.enabled = True
    settings.publishing.mode = "live"
    return settings


def published_post(session: Session, settings: Settings, tmp: Path, url: str) -> Post:
    campaign = make_campaign(session)
    campaign.platform_rates = [
        {
            "platform": "instagram",
            "rate_per_1k_usd": 1.5,
            "min_payout_usd": 4.0,
            "max_payout_usd": 30.0,
        }
    ]
    campaign.fee_bps = 1000
    campaign.submit_url = "https://whop.com/experiences/exp_x/campaigns/abc"
    add_account(session)
    approved_clip(session, settings, campaign, tmp)
    out = service.plan_posts(session, settings, now=NOW, host=FakeHost())
    pid = out["planned"][0]["post_id"]
    service.record_result(session, pid, ok=True, metricool_post_id="1")
    service.mark_published(session, pid, url)
    post = session.get(Post, pid)
    assert post is not None
    return post


def answer(url: str, views: int, likes: int, as_list: bool = False) -> dict[str, object]:
    values = {
        IG["url"]: url,
        IG["views"]: views,
        IG["likes"]: likes,
        IG["comments"]: 3,
        IG["shares"]: 1,
        IG["watch_time_s"]: 7.5,
    }
    row: object = [values[k] for k in REQ] if as_list else values
    return {"rows": [row, {IG["url"]: "https://www.instagram.com/reel/other/", IG["views"]: 9}]}


@pytest.mark.parametrize("as_list", [False, True])
def test_import_matches_by_normalized_url(
    session: Session, pub_settings: Settings, tmp_path: Path, as_list: bool
) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/AbC/")
    res = import_metrics(
        session,
        "instagram",
        REQ,
        answer("https://instagram.com/reel/AbC?igsh=1", 5000, 40, as_list),
    )
    assert res["matched"] == [{"post_id": post.id, "views": 5000, "likes": 40}]
    assert res["unmatched_rows"] == 1
    snap = latest_snapshots(session)[post.id or 0]
    assert snap.comments == 3 and snap.watch_time_s == 7.5


def test_latest_snapshot_wins(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/A/")
    t0 = datetime(2026, 11, 7, tzinfo=UTC)
    import_metrics(
        session, "instagram", REQ, answer(post.published_url or "", 100, 1), captured_at=t0
    )
    import_metrics(
        session,
        "instagram",
        REQ,
        answer(post.published_url or "", 900, 9),
        captured_at=t0 + timedelta(days=1),
    )
    assert latest_snapshots(session)[post.id or 0].views == 900


def test_earnings_threshold_cap_fee_and_engagement(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/E/")
    url = post.published_url or ""
    import_metrics(session, "instagram", REQ, answer(url, 2000, 5))
    [r] = estimate(session, pub_settings)
    assert r.net_usd == 0 and r.views_for_min_payout == 2667  # $3 < $4 minimum
    assert r.min_engagement_ok is False  # 0.25 % < 0.5 %

    import_metrics(session, "instagram", REQ, answer(url, 10_000, 100))
    [r] = estimate(session, pub_settings)
    assert r.gross_usd == pytest.approx(15.0) and r.net_usd == pytest.approx(13.5)
    assert r.min_engagement_ok is True and not r.capped

    import_metrics(session, "instagram", REQ, answer(url, 100_000, 1000))
    [r] = estimate(session, pub_settings)
    assert r.capped and r.net_usd == pytest.approx(27.0)  # $30 cap minus 10 % fee
    assert totals([r])["net_usd"] == pytest.approx(27.0)


def test_submission_flow(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/S/")
    [group] = submit.pending(session)
    assert group["submit_url"].startswith("https://whop.com/experiences/")
    assert group["posts"][0]["url"] == post.published_url
    sheet = submit.write_sheet(pub_settings, [group], now=NOW)
    assert (post.published_url or "") in sheet.read_text()

    sub = submit.mark(session, post.id or 0)
    assert sub.whop_status == "submitted" and sub.submitted_at is not None
    assert submit.pending(session) == []
    submit.mark(session, post.id or 0, "approved")
    with pytest.raises(ValueError):
        submit.mark(session, post.id or 0, "bogus")


def test_submit_moves_clip_to_tracking(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/T/")
    submit.mark(session, post.id or 0)
    from clipforge.daily import _clip

    assert _clip(session, post).status is ClipStatus.TRACKING
    assert post.status is PostStatus.PUBLISHED


def test_weekly_report_contents(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    post = published_post(session, pub_settings, tmp_path, "https://www.instagram.com/reel/W/")
    import_metrics(session, "instagram", REQ, answer(post.published_url or "", 1000, 2))
    text = weekly(session, pub_settings, now=NOW + timedelta(days=2))
    assert "Wochenbericht" in text and "1,000" in text
    assert "Noch bei Whop einzureichen" in text and (post.published_url or "") in text
    assert "Engagement" in text and "unter 0,5 %" in text
