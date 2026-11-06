"""Application settings: non-secret config from YAML, secrets from the environment / .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = "config.yaml"


class PathsConfig(BaseModel):
    data_dir: Path = Path("data")

    @property
    def sources(self) -> Path:
        return self.data_dir / "sources"

    @property
    def renders(self) -> Path:
        return self.data_dir / "renders"

    @property
    def cache(self) -> Path:
        return self.data_dir / "cache"

    @property
    def exports(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "clipforge.db"

    def all_dirs(self) -> list[Path]:
        return [self.data_dir, self.sources, self.renders, self.cache, self.exports, self.logs]


class PublishingConfig(BaseModel):
    enabled: bool = False
    approver: Literal["agent", "human"] = "agent"
    min_agent_score: float = Field(default=7.0, ge=0, le=10)


class RateLimitConfig(BaseModel):
    max_posts_per_account_per_day: int = Field(default=1, ge=1, le=10)
    min_gap_minutes: int = Field(default=120, ge=30)


class DisclosureConfig(BaseModel):
    texts: dict[str, str] = Field(default_factory=lambda: {"en": "#ad", "de": "Werbung"})

    def for_language(self, language: str) -> str:
        text = self.texts.get(language) or self.texts.get("en")
        if not text:
            raise ValueError(f"no disclosure text configured for language {language!r}")
        return text


class ClipLengthConfig(BaseModel):
    min_seconds: float = Field(default=20, gt=0)
    max_seconds: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> ClipLengthConfig:
        if self.min_seconds >= self.max_seconds:
            raise ValueError("clips.min_seconds must be smaller than clips.max_seconds")
        return self


class HighlightConfig(BaseModel):
    candidate_count: int = Field(default=40, ge=1)
    top_n_for_review: int = Field(default=15, ge=1)
    non_obvious_share: float = Field(default=0.2, ge=0, le=1)
    variants_per_moment: int = Field(default=2, ge=1, le=5)


class TranscriptionConfig(BaseModel):
    backend: Literal["auto", "mlx", "faster_whisper"] = "auto"
    model: str = "small.en"


class CampaignConfig(BaseModel):
    blocked_categories: list[str] = Field(default_factory=lambda: ["gambling", "casino"])
    blocked_keywords: list[str] = Field(
        default_factory=lambda: [
            "casino", "gambling", "betting", "sportsbook", "parlay", "slots", "poker",
            "roulette", "blackjack", "stake.com", "flip.gg", "case opening", "case battle",
            "skin gambling", "csgoroll", "rollbit",
            # adult / NSFW
            "sinparty", "onlyfans", "fansly", "nsfw", "porn", "xxx", "adult content", "18+",
        ]
    )  # fmt: skip
    risk_keywords: list[str] = Field(
        default_factory=lambda: [
            "glp-1", "weight loss", "hair restoration", "supplement", "trading", "forex",
            "crypto", "funded", "prop firm", "politic", "election",
        ]
    )  # fmt: skip
    content_language: str = "en"
    max_active: int = Field(default=3, ge=1)
    min_budget_left_usd: float = Field(default=300, ge=0)
    discover_sections: list[str] = Field(default_factory=lambda: ["clipping"])
    detail_top_n: int = Field(default=25, ge=1)


class AnalyticsConfig(BaseModel):
    whop_fee_pct: float = Field(default=7.0, ge=0, le=100)
    default_median_views: int = Field(default=1000, ge=0)


class Settings(BaseSettings):
    """All settings. Secrets come only from env / .env, never from YAML."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # --- non-secret (YAML) ---
    paths: PathsConfig = Field(default_factory=PathsConfig)
    language: str = "en"
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    disclosure: DisclosureConfig = Field(default_factory=DisclosureConfig)
    clips: ClipLengthConfig = Field(default_factory=ClipLengthConfig)
    highlights: HighlightConfig = Field(default_factory=HighlightConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    campaigns: CampaignConfig = Field(default_factory=CampaignConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)

    # --- secrets (env only) ---
    apify_token: SecretStr | None = None
    google_service_account_file: Path | None = None
    anthropic_api_key: SecretStr | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_path = Path(os.environ.get("CLIPFORGE_CONFIG", DEFAULT_CONFIG_FILE))
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path)
        # Precedence: explicit init > env > .env > YAML file.
        return (init_settings, env_settings, dotenv_settings, yaml_source)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.paths.db_file}"

    def ensure_dirs(self) -> None:
        for directory in self.paths.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
