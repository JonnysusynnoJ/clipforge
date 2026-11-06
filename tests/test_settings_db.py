from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, inspect
from sqlmodel import SQLModel

from clipforge.core import db
from clipforge.core.settings import Settings


def test_defaults_are_conservative() -> None:
    s = Settings()
    assert s.publishing.enabled is False
    assert s.rate_limits.max_posts_per_account_per_day == 1
    assert s.rate_limits.min_gap_minutes == 120
    assert s.disclosure.for_language("en") == "#ad"
    assert "gambling" in s.campaigns.blocked_categories


def test_yaml_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("publishing:\n  enabled: true\nrate_limits:\n  min_gap_minutes: 180\n")
    monkeypatch.setenv("CLIPFORGE_CONFIG", str(cfg))
    s = Settings()
    assert s.publishing.enabled is True
    assert s.rate_limits.min_gap_minutes == 180


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("publishing:\n  enabled: true\n")
    monkeypatch.setenv("CLIPFORGE_CONFIG", str(cfg))
    monkeypatch.setenv("PUBLISHING__ENABLED", "false")
    assert Settings().publishing.enabled is False


def test_secrets_from_env_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "secret-value")
    s = Settings()
    assert s.apify_token is not None
    assert s.apify_token.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(s)


def test_invalid_values_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(clips={"min_seconds": 60, "max_seconds": 20})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(rate_limits={"min_gap_minutes": 5})  # type: ignore[arg-type]


def test_migration_matches_models(engine: Engine, settings: Settings) -> None:
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(SQLModel.metadata.tables)
    assert db.current_revision(engine) == db.head_revision(settings.database_url)


def test_migration_columns_match_models(engine: Engine) -> None:
    insp = inspect(engine)
    for name, table in SQLModel.metadata.tables.items():
        db_cols = {c["name"] for c in insp.get_columns(name)}
        assert db_cols == {c.name for c in table.columns}, name
