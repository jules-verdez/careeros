from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from careeros.container import Container
from careeros.database.models import (
    Company,
    DailyBrief,
    Job,
    ResearchReport,
    Task,
)
from careeros.services.normalization import job_fingerprint, normalize_text


def create_job(container: Container, *, company_name: str, title: str) -> str:
    with container.database.session() as session:
        company = Company(
            canonical_name=company_name,
            normalized_name=normalize_text(company_name),
            domain=f"{normalize_text(company_name).replace(' ', '-')}.test",
        )
        session.add(company)
        session.flush()
        job = Job(
            company_id=company.id,
            canonical_fingerprint=job_fingerprint(
                company_identity=company_name,
                title=title,
                location="London, UK",
                posted_at=None,
            ),
            title=title,
            normalized_title=normalize_text(title),
            location="London, UK",
        )
        session.add(job)
        session.flush()
        return job.id


def test_assessment_ranking_and_shortlist_generates_research_task(
    container: Container,
    active_profile: str,
) -> None:
    better = create_job(
        container,
        company_name="Example Capital",
        title="Quantitative Researcher",
    )
    worse = create_job(
        container,
        company_name="Signal Systems",
        title="Quant Platform Engineer",
    )
    with container.database.session() as session:
        container.assessments.assess(
            session,
            job_id=better,
            profile_id=active_profile,
            fit_score=0.9,
            confidence=0.9,
            competition_score=0.6,
            explanation={"rationale": "verified"},
            actor="test",
        )
        container.assessments.assess(
            session,
            job_id=worse,
            profile_id=active_profile,
            fit_score=0.6,
            confidence=0.8,
            competition_score=0.5,
            explanation={"rationale": "verified"},
            actor="test",
        )
        assert container.assessments.rank(session, profile_id=active_profile)[0].job.id == better
        container.assessments.shortlist(session, job_id=better, actor="test")

    result = container.daily_agent.run(profile_id=active_profile, limit=5)
    assert result.view.items[0].recommendation == "Research Example Capital"
    assert result.view.brief.estimated_minutes > 0

    second = container.daily_agent.run(profile_id=active_profile, limit=5)
    assert second.view.brief.revision == 2
    with container.database.session() as session:
        assert session.scalar(select(func.count(DailyBrief.id))) == 2
        research_tasks = session.scalars(
            select(Task).where(Task.title == "Research Example Capital")
        ).all()
        assert len(research_tasks) == 1


def test_research_snapshot_is_immutable_per_company_and_date(
    container: Container,
    active_profile: str,
) -> None:
    del active_profile
    create_job(
        container,
        company_name="Example Capital",
        title="Quantitative Researcher",
    )
    with container.database.session() as session:
        report = container.research.import_snapshot(
            session,
            company_name="Example Capital",
            as_of_date=date(2026, 7, 30),
            markdown="# Evidence\n\nCited facts.",
            structured={"claims": [], "unknowns": []},
            confidence=0.8,
            actor="test",
        )
        assert session.get(ResearchReport, report.id) is not None
    with container.database.session() as session:
        with pytest.raises(ValueError, match="already exists"):
            container.research.import_snapshot(
                session,
                company_name="Example Capital",
                as_of_date=date(2026, 7, 30),
                markdown="# Replacement",
                structured={"claims": []},
                confidence=0.9,
                actor="test",
            )


def test_invalid_assessment_score_is_rejected(
    container: Container,
    active_profile: str,
) -> None:
    job_id = create_job(container, company_name="Example", title="Researcher")
    with container.database.session() as session:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            container.assessments.assess(
                session,
                job_id=job_id,
                profile_id=active_profile,
                fit_score=1.1,
                confidence=0.8,
                competition_score=0.5,
                explanation={},
                actor="test",
            )
