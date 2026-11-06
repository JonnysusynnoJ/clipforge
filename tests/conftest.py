from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from clipforge.core import db
from clipforge.core.models import Campaign, ClipCandidate, SourceAsset
from clipforge.core.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never pick up the developer's config.yaml / .env in tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIPFORGE_CONFIG", str(tmp_path / "config.yaml"))
    for var in ("APIFY_TOKEN", "GOOGLE_SERVICE_ACCOUNT_FILE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(paths={"data_dir": tmp_path / "data"})  # type: ignore[arg-type]


@pytest.fixture
def engine(settings: Settings) -> Engine:
    settings.ensure_dirs()
    db.upgrade(settings.database_url)
    return db.make_engine(settings.database_url)


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def clip(session: Session) -> ClipCandidate:
    campaign = Campaign(whop_url="https://whop.com/example/campaign", brand="Example")
    session.add(campaign)
    session.flush()
    asset = SourceAsset(campaign_id=campaign.id or 0, origin_url="file:///synthetic.mp4")
    session.add(asset)
    session.flush()
    c = ClipCandidate(asset_id=asset.id or 0, start=10.0, end=40.0)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c
