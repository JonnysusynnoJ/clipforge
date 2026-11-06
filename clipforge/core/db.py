"""Database engine, sessions and migrations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

import clipforge
from clipforge.core import models  # noqa: F401  (register tables on SQLModel.metadata)

MIGRATIONS_DIR = Path(clipforge.__file__).parent / "migrations"


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, connect_args={"check_same_thread": False})


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def head_revision(database_url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        rev: str | None = MigrationContext.configure(conn).get_current_revision()
        return rev
