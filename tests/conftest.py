from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros.clock import FixedClock
from careeros.config import Settings
from careeros.container import Container
from careeros.database.base import Base

FIXED_NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def container(tmp_path: Path) -> Container:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'career.db'}",
        timezone="Europe/London",
        candidate_name="Demo Candidate",
    )
    result = Container.build(
        settings,
        clock=FixedClock(FIXED_NOW),
        prompt_root=PROJECT_ROOT / "prompts",
    )
    Base.metadata.create_all(result.database.engine)
    yield result
    result.database.dispose()


@pytest.fixture
def active_profile(container: Container) -> str:
    with container.database.session() as session:
        profile = container.profiles.ensure_profile(
            session,
            name="Demo Candidate",
            email="candidate@example.com",
            actor="test",
        )
        return profile.id
