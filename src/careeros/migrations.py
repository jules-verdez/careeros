"""Alembic migration entry points used by the CLI."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from careeros.resources import resource_root


def alembic_config(database_url: str) -> Config:
    root = resource_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def current_revision(database_url: str) -> None:
    command.current(alembic_config(database_url), verbose=True)


def revision_state(database_url: str, engine: Engine) -> tuple[str | None, str | None]:
    """Return the database's applied revision and the newest available revision."""
    head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current, head
