from __future__ import annotations

import pytest
from pydantic import ValidationError

from clipforge.campaigns.rules import ParsedRules
from clipforge.compliance.check import PostMetadata, check
from clipforge.core.models import Platform

SOURCE = "https://app.example.com/share/abc"

RULES = ParsedRules(
    allowed_platforms=[Platform.TIKTOK, Platform.INSTAGRAM],
    allowed_sources=[SOURCE],
    min_seconds=10,
    max_seconds=30,
    required_mentions=["@brand"],
    disclosure_options=["#Ad", "#Sponsored"],
    disclosure_own_line_first=True,
    max_extra_hashtags=3,
    caption_examples=["The new maps look INSANE"],
    forbidden_caption_terms=["gameplay"],
)

GOOD = "Season one looks great @brand\n#Ad\n#Game #Season1"


def run(caption: str = GOOD, *, duration: float = 20, origin: str = SOURCE,
        platforms: list[Platform] | None = None, note: str = "") -> list[str]:  # fmt: skip
    report = check(
        rules=RULES,
        duration=duration,
        origin_url=origin,
        license_note=note,
        metadata=PostMetadata(caption=caption, platforms=platforms or [Platform.INSTAGRAM]),
        overlays=[],
        disclosure_default="#ad",
    )
    return [v.rule for v in report.hard_violations]


def test_good_post_passes() -> None:
    assert run() == []


def test_source_prefix_match() -> None:
    assert RULES.source_allowed(SOURCE + "?x=1")
    assert not RULES.source_allowed("https://youtube.com/watch?v=1")


def test_rules_validation() -> None:
    with pytest.raises(ValidationError):
        ParsedRules(min_seconds=30, max_seconds=10)
    with pytest.raises(ValidationError):
        ParsedRules(disclosure_options=[])


@pytest.mark.parametrize(
    ("caption", "rule"),
    [
        ("Season one looks great @brand\n#Game", "disclosure"),
        ("Season one looks great @brand\n#Game\n#Ad", "disclosure"),  # not first hashtag
        ("Season one looks great @brand #Ad", "disclosure"),  # not on its own line
        ("Season one looks great\n#Ad", "mentions"),
        ("Looks great @brand\n#Ad\n#a #b #c #d", "hashtags"),
        ("Best gameplay ever @brand\n#Ad", "forbidden_terms"),
        ("The new maps look INSANE @brand\n#Ad", "caption_copy"),
        ("", "caption"),
    ],
)
def test_caption_violations(caption: str, rule: str) -> None:
    assert rule in run(caption)


def test_length_platform_source() -> None:
    assert "length" in run(duration=5)
    assert "length" in run(duration=45)
    assert "platform" in run(platforms=[Platform.YOUTUBE])
    assert "source" in run(origin="https://other.example/video.mp4")
    assert "source" not in run(origin="https://other.example/video.mp4", note="manual approval: ok")
