from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from careeros.database import models  # noqa: F401
from careeros.database.base import Base
from careeros.migrations import upgrade_database


def test_initial_migration_builds_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "migrated.db"
    url = f"sqlite:///{path}"
    upgrade_database(url)
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert "jobs" in inspector.get_table_names()
        assert "action_requests" in inspector.get_table_names()
        assert "audit_events" in inspector.get_table_names()
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "render_as_batch": True},
            )
            assert compare_metadata(context, Base.metadata) == []
            assert connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def test_upgrade_adopts_existing_applications_without_changing_history(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from alembic import command
    from sqlalchemy.orm import Session

    from careeros.database.enums import ApplicationStatus, JobStatus
    from careeros.database.models import (
        Application,
        ApplicationWorkspace,
        CandidateProfile,
        Company,
        Job,
    )
    from careeros.migrations import alembic_config

    url = f"sqlite:///{tmp_path / 'existing.db'}"
    upgrade_database(url, "e27f43a16b5c")
    engine = create_engine(url)
    submitted = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        profile = CandidateProfile(name="Demo Candidate", is_active=True)
        company = Company(canonical_name="Example", normalized_name="example")
        session.add_all([profile, company])
        session.flush()
        for index, status in enumerate((ApplicationStatus.APPLIED, ApplicationStatus.REJECTED)):
            job = Job(
                company_id=company.id,
                canonical_fingerprint=str(index),
                title="Researcher",
                normalized_title="researcher",
                status=JobStatus.APPLIED,
            )
            session.add(job)
            session.flush()
            session.add(
                Application(
                    job_id=job.id,
                    status=status,
                    applied_at=submitted,
                    next_followup_at=submitted if status == ApplicationStatus.APPLIED else None,
                    outcome_reason="Preserve this evidence",
                )
            )
    upgrade_database(url)
    upgrade_database(url)
    with Session(engine) as session:
        applications = session.query(Application).all()
        assert len(applications) == 2
        for application in applications:
            assert application.outcome_reason == "Preserve this evidence"
            assert application.applied_at.replace(tzinfo=UTC) == submitted
            workspace = session.get(ApplicationWorkspace, application.id)
            assert workspace is not None
            if application.status == ApplicationStatus.APPLIED:
                assert workspace.next_action == "Check for response"
                assert workspace.next_action_at is not None
            else:
                assert workspace.next_action is None
                assert workspace.next_action_at is None
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(alembic_config(url), "e27f43a16b5c")
    with Session(engine) as session:
        assert session.query(Application).count() == 2
    assert "application_workspaces" not in inspect(engine).get_table_names()
    engine.dispose()
