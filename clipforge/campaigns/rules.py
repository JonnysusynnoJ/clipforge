"""Structured campaign rules and the campaign spec file format (`--campaign-json`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from clipforge.core.models import Platform


class RenderRules(BaseModel):
    """Campaign-specific render requirements."""

    layout: Literal["auto", "blur_fit", "center_crop", "passthrough"] | None = None
    full_frame: bool = Field(
        default=False, description="Show the whole source frame (no crop), blurred fill around it"
    )
    safe_top: int | None = Field(default=None, ge=0, le=900)
    safe_bottom: int | None = Field(default=None, ge=0, le=900)
    safe_left: int | None = Field(default=None, ge=0, le=500)
    safe_right: int | None = Field(default=None, ge=0, le=500)
    end_card_file: str | None = Field(
        default=None, description="File name of the end card / packshot in the campaign assets"
    )
    end_card_seconds: float = Field(default=2.5, ge=0.5, le=6)
    keep_source_fps: bool = False
    caption_words_per_line: int | None = Field(default=None, ge=1, le=8)


class ParsedRules(BaseModel):
    """Machine-checkable rules extracted from a campaign briefing."""

    allowed_platforms: list[Platform] = Field(default_factory=list)
    allowed_sources: list[str] = Field(
        default_factory=list, description="URL prefixes of authorized source material"
    )
    min_seconds: float | None = None
    max_seconds: float | None = None
    required_mentions: list[str] = Field(default_factory=list)  # e.g. ["@callofduty"]
    required_mentions_by_platform: dict[Platform, list[str]] = Field(
        default_factory=dict, description="Extra mentions only for one platform"
    )
    required_hashtags: list[str] = Field(default_factory=list)
    disclosure_options: list[str] = Field(default_factory=lambda: ["#ad"])
    disclosure_own_line_first: bool = False
    max_extra_hashtags: int | None = None
    required_overlays: list[str] = Field(default_factory=list)
    on_screen_text_examples: list[str] = Field(default_factory=list)
    caption_examples: list[str] = Field(default_factory=list)
    forbidden_caption_terms: list[str] = Field(default_factory=list)
    forbidden_content: list[str] = Field(default_factory=list)
    music_only_from_source: bool = False
    no_ai_voice: bool = False
    language: str = "en"
    one_account_per_video: bool = False
    max_posts_per_video: int | None = None
    min_live_days: int | None = None
    geo_restrictions: list[str] = Field(default_factory=list)
    min_payout_usd: float | None = None
    cap_per_clip_usd: float | None = None
    render: RenderRules = Field(default_factory=RenderRules)

    @model_validator(mode="after")
    def _check(self) -> ParsedRules:
        if self.min_seconds and self.max_seconds and self.min_seconds >= self.max_seconds:
            raise ValueError("min_seconds must be smaller than max_seconds")
        if not self.disclosure_options:
            raise ValueError("at least one disclosure option is required")
        return self

    def source_allowed(self, origin_url: str) -> bool:
        return any(origin_url.startswith(prefix) for prefix in self.allowed_sources)


class CampaignSpec(BaseModel):
    """File format for a campaign: metadata + raw rules text + parsed rules."""

    whop_url: str
    brand: str
    category: str | None = None
    cpm_usd: float | None = None
    budget_total: float | None = None
    budget_left: float | None = None
    raw_rules_text: str = ""
    rules: ParsedRules

    @classmethod
    def load(cls, path: Path) -> CampaignSpec:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
