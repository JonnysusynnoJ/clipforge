from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from clipforge.campaigns.rules import CampaignSpec, ParsedRules, RenderRules
from clipforge.compliance.check import PostMetadata, check
from clipforge.core.models import ClipStatus, Platform
from clipforge.core.settings import Settings
from clipforge.ingest import gdrive
from clipforge.ingest.local import import_file
from clipforge.media import probe
from clipforge.pipeline import (
    CandidateScore,
    apply_scores,
    campaign_assets_dir,
    stage_candidates,
    stage_render,
    upsert_campaign,
)
from clipforge.transcribe.whisper import TranscriptResult
from tests.fixtures import make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
SOURCE = "https://drive.google.com/file/d/abcdefghijklmnop"


def test_drive_file_id_parsing() -> None:
    assert gdrive.file_id("https://drive.google.com/file/d/1yDzxxDk-QSC_9/view?usp=sharing") == (
        "1yDzxxDk-QSC_9"
    )
    assert gdrive.file_id("https://drive.google.com/open?id=abcdefghijk12") == "abcdefghijk12"
    assert gdrive.is_drive_file(SOURCE)
    assert not gdrive.is_drive_file("https://youtube.com/watch?v=x")


def test_per_platform_mentions() -> None:
    rules = ParsedRules(
        allowed_platforms=[Platform.INSTAGRAM, Platform.YOUTUBE],
        allowed_sources=[SOURCE],
        required_mentions_by_platform={
            Platform.INSTAGRAM: ["@Clash.HoH"],
            Platform.YOUTUBE: ["@Clash"],
        },
        disclosure_options=["#ad"],
    )

    def violations(caption: str, platform: Platform) -> list[str]:
        report = check(
            rules=rules,
            duration=20,
            origin_url=SOURCE,
            license_note="",
            metadata=PostMetadata(caption=caption, platforms=[platform]),
            overlays=[],
            disclosure_default="#ad",
        )
        return [v.message for v in report.hard_violations]

    assert violations("Wild trivia @Clash.HoH #ad", Platform.INSTAGRAM) == []
    assert violations("Wild trivia @Clash #ad", Platform.INSTAGRAM)
    assert violations("Wild trivia @Clash #ad", Platform.YOUTUBE) == []


def test_end_card_required_by_compliance() -> None:
    rules = ParsedRules(
        allowed_platforms=[Platform.TIKTOK],
        allowed_sources=[SOURCE],
        render=RenderRules(end_card_file="packshot.png"),
    )
    meta = PostMetadata(caption="Tip #ad", platforms=[Platform.TIKTOK])
    base = dict(
        rules=rules,
        duration=20,
        origin_url=SOURCE,
        license_note="",
        metadata=meta,
        overlays=[],
        disclosure_default="#ad",
    )
    assert "end_card" in [v.rule for v in check(**base, has_end_card=False).hard_violations]  # type: ignore[arg-type]
    assert check(**base, has_end_card=True).passed  # type: ignore[arg-type]


def _setup(session: Session, settings: Settings, tmp: Path, render: RenderRules):  # type: ignore[no-untyped-def]
    spec = CampaignSpec(
        whop_url="https://contentrewards.com/c/campaigns/zz",
        brand="Zutobi",
        rules=ParsedRules(
            allowed_platforms=[Platform.TIKTOK],
            allowed_sources=[SOURCE],
            min_seconds=6,
            max_seconds=12,
            render=render,
        ),
    )
    campaign = upsert_campaign(session, spec)
    video = make_video(tmp / "src.mp4", seconds=16)
    asset = import_file(
        session, campaign, video, sources_dir=settings.paths.sources, origin_url=SOURCE
    )
    settings.highlights.top_n_for_review = 1
    settings.highlights.variants_per_moment = 1
    empty = TranscriptResult(language="en", model="none", backend="none")
    rows, _ = stage_candidates(
        session, settings, campaign=campaign, asset=asset, transcript=empty, run_id="rr"
    )
    apply_scores(session, settings, "rr", [CandidateScore(id=rows[0].id or 0, score=9)])
    return campaign, rows[0]


@needs_ffmpeg
def test_render_rules_end_card_fps_full_frame(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    render = RenderRules(
        full_frame=True,
        keep_source_fps=True,
        end_card_file="packshot.png",
        end_card_seconds=2.0,
        safe_right=150,
    )
    campaign, clip = _setup(session, settings, tmp_path, render)
    assets = campaign_assets_dir(settings, campaign)
    assets.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:d=1",
            "-frames:v",
            "1",
            str(assets / "packshot.png"),
        ],
        check=True,
    )
    [row] = stage_render(session, settings, "rr", max_clips=1)
    info = probe(Path(row.path))
    assert info.fps == pytest.approx(25, abs=0.1)  # fixture source is 25 fps
    assert info.duration == pytest.approx(clip.end - clip.start + 2.0, abs=0.3)
    assert row.overlay_config["end_card"] == "packshot.png"
    assert clip.status is ClipStatus.PENDING_REVIEW


@needs_ffmpeg
def test_missing_end_card_fails_clip(session: Session, settings: Settings, tmp_path: Path) -> None:
    _, clip = _setup(session, settings, tmp_path, RenderRules(end_card_file="missing.png"))
    assert stage_render(session, settings, "rr", max_clips=1) == []
    assert clip.status is ClipStatus.FAILED and "end card" in (clip.error or "")
