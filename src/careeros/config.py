"""Runtime configuration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application settings.

    Secrets belong in connector-specific environment variables. Core settings are
    intentionally small so the domain layer does not depend on any provider.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CAREEROS_",
        extra="ignore",
    )

    database_url: str = Field(default="sqlite:///career.db")
    timezone: str = Field(default="UTC")
    candidate_name: str = Field(default="Candidate")
    approval_required: bool = Field(default=True)
    #: People CareerOS must never draft for, queue, or send to. Each entry is an exact
    #: address, an ``@domain``, or a person's full name. Approval cannot override it.
    do_not_contact: Annotated[tuple[str, ...], NoDecode] = Field(default=())

    @field_validator("do_not_contact", mode="before")
    @classmethod
    def split_do_not_contact(cls, value: object) -> object:
        """Accept one delimited line so the list can live in ``.env``."""
        if isinstance(value, str):
            return tuple(part.strip() for part in re.split(r"[,;\n]", value) if part.strip())
        return value

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_sqlalchemy_url(cls, value: str) -> str:
        if "://" not in value:
            msg = "database_url must be a SQLAlchemy URL, for example sqlite:///career.db"
            raise ValueError(msg)
        return value

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix)).expanduser()


def load_environment() -> None:
    """Read only the working directory's explicit .env; never search parent folders."""
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
