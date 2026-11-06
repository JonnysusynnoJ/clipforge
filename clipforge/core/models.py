"""Domain models (SQLModel tables) and enums."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(nullable: bool = False, **kwargs: Any) -> Any:
    """Timezone-aware timestamp column."""
    return Field(sa_type=DateTime(timezone=True), nullable=nullable, **kwargs)


def _json(default_factory: Any) -> Any:
    return Field(default_factory=default_factory, sa_type=JSON)


# --------------------------------------------------------------------------- enums


class Platform(StrEnum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"


class Actor(StrEnum):
    SYSTEM = "system"  # deterministic pipeline code
    AGENT = "agent"  # Claude review agent
    HUMAN = "human"  # the owner


class CampaignStatus(StrEnum):
    DISCOVERED = "discovered"
    ACTIVE = "active"
    PAUSED = "paused"
    EXHAUSTED = "exhausted"
    IGNORED = "ignored"


class ClipStatus(StrEnum):
    CANDIDATE = "candidate"
    RENDERED = "rendered"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    SUBMITTED = "submitted"
    TRACKING = "tracking"
    CLOSED = "closed"
    FAILED = "failed"


class PostStatus(StrEnum):
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------- tables


class Campaign(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    whop_url: str = Field(index=True, unique=True)
    external_id: str | None = Field(default=None, index=True)
    title: str = ""
    brand: str
    category: str | None = None
    description: str = ""
    payout_type: str = "cpm"
    platform_rates: list[dict[str, Any]] = _json(list)
    fee_bps: int | None = None
    requires_application: bool = False
    launched_at: datetime | None = _ts(nullable=True, default=None)
    submit_url: str | None = None
    brief_url: str | None = None
    score: float | None = None
    score_report: dict[str, Any] = _json(dict)
    cpm_usd: float | None = None
    budget_total: float | None = None
    budget_left: float | None = None
    platforms: list[str] = _json(list)
    geo_restrictions: list[str] = _json(list)
    raw_rules_text: str = ""
    parsed_rules: dict[str, Any] = _json(dict)
    status: CampaignStatus = Field(default=CampaignStatus.DISCOVERED, index=True)
    fetched_at: datetime = _ts(default_factory=utcnow)


class SourceAsset(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    origin_url: str
    local_path: str | None = None
    sha256: str | None = Field(default=None, index=True)
    duration: float | None = None
    license_note: str = ""
    created_at: datetime = _ts(default_factory=utcnow)


class Transcript(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="sourceasset.id", index=True)
    language: str | None = None
    model: str = ""
    words: list[dict[str, Any]] = _json(list)  # {"w", "start", "end", "conf"}
    segments: list[dict[str, Any]] = _json(list)
    created_at: datetime = _ts(default_factory=utcnow)


class ClipCandidate(SQLModel, table=True):
    """A clip moment; its `status` is the clip lifecycle (see core.state)."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="sourceasset.id", index=True)
    start: float
    end: float
    features: dict[str, Any] = _json(dict)
    llm_score: float | None = None
    llm_reasoning: str | None = None
    hook_text: str | None = None
    status: ClipStatus = Field(default=ClipStatus.CANDIDATE, index=True)
    error: str | None = None
    run_id: str | None = Field(default=None, index=True)
    created_at: datetime = _ts(default_factory=utcnow)
    updated_at: datetime = _ts(default_factory=utcnow)


class RenderedClip(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="clipcandidate.id", index=True)
    path: str
    platform_profile: str
    caption_style: str = "default"
    overlay_config: dict[str, Any] = _json(dict)
    compliance_report: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    agent_review: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    post_metadata: dict[str, Any] = _json(dict)
    run_id: str | None = Field(default=None, index=True)
    public_url: str | None = None
    created_at: datetime = _ts(default_factory=utcnow)


class SocialAccount(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    platform: Platform
    handle: str
    metricool_blog_id: str | None = None
    whop_linked: bool = False
    is_public: bool = True
    active: bool = True
    daily_limit: int | None = None  # overrides the global rate limit if lower


class Post(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rendered_clip_id: int = Field(foreign_key="renderedclip.id", index=True)
    account_id: int = Field(foreign_key="socialaccount.id", index=True)
    platform: Platform | None = None
    scheduled_at: datetime = _ts()
    caption: str = ""
    disclosure_text: str
    media_url: str | None = None
    media_key: str | None = None
    publisher: str = "metricool"
    draft: bool = True
    request_payload: dict[str, Any] = _json(dict)
    metricool_post_id: str | None = None
    planner_url: str | None = None
    published_url: str | None = None
    error: str | None = None
    status: PostStatus = Field(default=PostStatus.PLANNED, index=True)
    created_at: datetime = _ts(default_factory=utcnow)


class Submission(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    submitted_at: datetime | None = _ts(nullable=True, default=None)
    whop_status: str | None = None
    notes: str = ""


class MetricSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    captured_at: datetime = _ts(default_factory=utcnow)
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    watch_time_s: float | None = None


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    entity_type: str = Field(index=True)
    entity_id: int = Field(index=True)
    from_status: str
    to_status: str
    actor: Actor
    reason: str = ""
    created_at: datetime = _ts(default_factory=utcnow)


class UsageLog(SQLModel, table=True):
    """Cost / usage protocol for LLM or agent steps."""

    id: int | None = Field(default=None, primary_key=True)
    step: str = Field(index=True)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime = _ts(default_factory=utcnow)
