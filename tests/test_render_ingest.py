from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from clipforge.campaigns.rules import CampaignSpec, ParsedRules
from clipforge.core.models import ClipStatus, Platform
from clipforge.core.settings import Settings
from clipforge.ingest import mediasilo
from clipforge.ingest.local import UnauthorizedSourceError, import_file
from clipforge.media import probe
from clipforge.pipeline import (
    CandidateScore,
    ReviewDecision,
    apply_review,
    apply_scores,
    stage_candidates,
    stage_render,
    upsert_campaign,
)
from clipforge.render.captions import PRESETS, build_ass
from clipforge.render.profiles import UNIVERSAL
from clipforge.transcribe.whisper import TranscriptResult, Word
from tests.fixtures import make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
SOURCE = "https://share.example.com/campaign-folder"


def _spec() -> CampaignSpec:
    return CampaignSpec(
        whop_url="https://whop.com/test/campaign",
        brand="Test",
        rules=ParsedRules(
            allowed_platforms=[Platform.INSTAGRAM],
            allowed_sources=[SOURCE],
            min_seconds=6,
            max_seconds=12,
            required_mentions=["@brand"],
            disclosure_options=["#Ad"],
            disclosure_own_line_first=True,
            max_extra_hashtags=3,
        ),
    )


def test_ass_contains_hook_credits_and_word_highlight() -> None:
    words = [Word("hello", 0.0, 0.4, 1), Word("big", 0.5, 0.8, 1), Word("world.", 0.9, 1.3, 1)]
    ass = build_ass(
        profile=UNIVERSAL,
        duration=10,
        words=words,
        style=PRESETS["default"],
        hook_text="Watch {this}",
        credits=["Credit: Studio"],
    )
    assert "Hook,,0,0,0,,{\\fad(120,200)}Watch (this)" in ass
    assert "Credit: Studio" in ass
    assert ass.count("Dialogue: 0,") == 3  # one event per highlighted word
    assert "HELLO" in ass


def test_mediasilo_parser_picks_preferred_rendition() -> None:
    def url(suffix: str) -> str:
        return (
            f"https://abc123.cloudfront.net/x/id{suffix}.mp4?response-content-disposition="
            "attachment%3Bfilename%3D%22Trailer%2Emp4%22&Expires=1&Signature=s"
        )

    page = '"allowDownload":true ' + " ".join(f'"{url(s)}"' for s in ("x720", "x1080", "x2000"))
    [asset] = mediasilo.parse_assets(page, prefer_height=1080)
    assert asset.file_name == "Trailer.mp4" and "x1080" in asset.download_url and asset.is_video
    assert mediasilo.parse_assets(page.replace("true", "false")) == []


@needs_ffmpeg
def test_ingest_requires_authorization_and_dedupes(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    video = make_video(tmp_path / "in.mp4", seconds=8)
    campaign = upsert_campaign(session, _spec())
    with pytest.raises(UnauthorizedSourceError):
        import_file(
            session,
            campaign,
            video,
            sources_dir=settings.paths.sources,
            origin_url="https://random.example/v.mp4",
        )
    a1 = import_file(
        session, campaign, video, sources_dir=settings.paths.sources, origin_url=SOURCE
    )
    a2 = import_file(
        session, campaign, video, sources_dir=settings.paths.sources, origin_url=SOURCE
    )
    assert a1.id == a2.id and a1.duration == pytest.approx(8, abs=0.2)
    manual = import_file(
        session,
        campaign,
        make_video(tmp_path / "b.mp4", seconds=4),
        sources_dir=settings.paths.sources,
        origin_url="https://random.example/b.mp4",
        license_note="owner ok",
    )
    assert manual.license_note.startswith("manual approval")


@needs_ffmpeg
def test_end_to_end_agent_flow(session: Session, settings: Settings, tmp_path: Path) -> None:
    settings.highlights.top_n_for_review = 3
    settings.highlights.variants_per_moment = 2
    video = make_video(tmp_path / "src.mp4", seconds=24)
    campaign = upsert_campaign(session, _spec())
    asset = import_file(
        session, campaign, video, sources_dir=settings.paths.sources, origin_url=SOURCE
    )
    empty = TranscriptResult(language="en", model="none", backend="none")
    rows, _ = stage_candidates(
        session, settings, campaign=campaign, asset=asset, transcript=empty, run_id="t1"
    )
    assert rows
    doc = json.loads((settings.paths.data_dir / "runs/t1/candidates.json").read_text())
    assert all(Path(c["contact_sheet"]).exists() for c in doc["candidates"])

    roots = [r for r in rows if r.features["variant_of"] is None]
    best, skipped = roots[0], roots[-1]
    apply_scores(
        session,
        settings,
        "t1",
        [
            CandidateScore(id=best.id or 0, score=8.5, hook_text="Big moment", decision="render"),
            CandidateScore(id=skipped.id or 0, score=2, decision="skip", reasoning="weak"),
        ],
    )
    rendered = stage_render(session, settings, "t1", max_clips=5)
    assert [r.candidate_id for r in rendered] == [best.id]  # only what the agent scored
    assert skipped.status is ClipStatus.REJECTED

    out = Path(rendered[0].path)
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    loud = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(out), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    integrated = float(loud.split("Integrated loudness:")[1].split("I:")[1].split("LUFS")[0])
    assert integrated == pytest.approx(-14, abs=1.5)
    assert best.status is ClipStatus.PENDING_REVIEW
    assert rendered[0].compliance_report is not None  # caption still missing -> not passed

    bad = apply_review(
        session,
        settings,
        "t1",
        [
            ReviewDecision(
                clip_id=best.id or 0, decision="approve", score=9, caption="nice clip #Ad"
            )
        ],
    )
    assert bad["blocked"] and best.status is ClipStatus.PENDING_REVIEW

    good = apply_review(
        session,
        settings,
        "t1",
        [
            ReviewDecision(
                clip_id=best.id or 0,
                decision="approve",
                score=9,
                caption="What a moment @brand\n#Ad\n#Test",
            )
        ],
    )
    assert good["approved"] and best.status is ClipStatus.APPROVED


@needs_ffmpeg
def test_low_agent_score_is_rejected(session: Session, settings: Settings, tmp_path: Path) -> None:
    settings.highlights.top_n_for_review = 1
    video = make_video(tmp_path / "src.mp4", seconds=16)
    campaign = upsert_campaign(session, _spec())
    asset = import_file(
        session, campaign, video, sources_dir=settings.paths.sources, origin_url=SOURCE
    )
    empty = TranscriptResult(language="en", model="none", backend="none")
    rows, _ = stage_candidates(
        session, settings, campaign=campaign, asset=asset, transcript=empty, run_id="t2"
    )
    stage_render(session, settings, "t2", max_clips=1)
    clip = next(r for r in rows if r.status is ClipStatus.PENDING_REVIEW)
    res = apply_review(
        session,
        settings,
        "t2",
        [
            ReviewDecision(
                clip_id=clip.id or 0, decision="approve", score=5, caption="Fine @brand\n#Ad"
            )
        ],
    )
    assert res["rejected"] and clip.status is ClipStatus.REJECTED
