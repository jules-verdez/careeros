from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import func, select

from careeros.cli_views import render_opportunities
from careeros.container import Container
from careeros.database.base import Base
from careeros.database.enums import JobStatus, SourceKind, TaskStatus
from careeros.database.models import AgentRun, CandidateProfile, Job, JobAssessment, Task
from careeros.domain.job_review import EligibilityLabel, ReviewEvidence
from careeros.domain.records import JobRecord
from careeros.services.opportunities import OpportunityOrder


def add_role(container: Container, title: str = "Researcher", *, days_ago: int = 0) -> str:
    with container.database.session() as session:
        container.ingestion.ingest(
            session,
            [
                JobRecord(
                    source_kind=SourceKind.GREENHOUSE,
                    source_name="Example Careers",
                    external_id=title,
                    source_url="https://example.test/jobs/researcher",
                    company_name="Example Capital",
                    title=title,
                    location="London, UK",
                )
            ],
            actor="test",
            connector_name="test-local",
        )
        job = session.scalar(select(Job).where(Job.title == title))
        assert job is not None
        job.first_seen_at = container.clock.now() - timedelta(days=days_ago)
        return job.id


def assess(
    container: Container,
    profile_id: str,
    job_id: str,
    *,
    fit: float = 0.9,
    explanation: dict[str, Any] | None = None,
    version: str = "test-v1",
    days_ago: int = 0,
    review: ReviewEvidence | None = None,
) -> str:
    with container.database.session() as session:
        assessment = container.assessments.assess(
            session,
            job_id=job_id,
            profile_id=profile_id,
            fit_score=fit,
            confidence=0.9,
            competition_score=0.5,
            explanation=explanation or {},
            scoring_version=version,
            actor="test",
            review=review,
        )
        assessment.assessed_at = container.clock.now() - timedelta(days=days_ago)
        return assessment.id


@pytest.mark.parametrize("fit,forced", [(0.75, False), (0.750000001, True)])
def test_strict_review_boundary_cannot_be_suppressed_by_legacy_flag(
    container: Container, active_profile: str, fit: float, forced: bool
) -> None:
    job_id = add_role(container)
    assess(container, active_profile, job_id, fit=fit, explanation={"mandatory_user_review": False})
    with container.database.session() as session:
        queue = container.opportunities.queue(session, profile_id=active_profile)
    assert bool(queue.mandatory_user_review) is forced
    card = (queue.mandatory_user_review or queue.opportunities)[0]
    assert card.profile_fit_score == fit
    assert card.eligibility_label == EligibilityLabel.UNKNOWN
    assert card.canonical_url == "https://example.test/jobs/researcher"
    assert "recorded work rights" in card.unresolved_fact
    output = StringIO()
    render_opportunities(Console(file=output, width=160), queue)
    assert str(fit) in output.getvalue()


def test_latest_assessment_wins_before_ranking_and_is_scoped_to_profile(
    container: Container, active_profile: str
) -> None:
    job_id = add_role(container)
    assess(container, active_profile, job_id, fit=0.99, version="old", days_ago=1)
    current = assess(container, active_profile, job_id, fit=0.6, version="new")
    with container.database.session() as session:
        other = CandidateProfile(name="Other", is_active=False)
        session.add(other)
        session.flush()
        other_id = other.id
    assess(container, other_id, job_id, fit=1, version="other")
    with container.database.session() as session:
        ranked = container.assessments.rank(session, profile_id=active_profile)
        queue = container.opportunities.queue(session, profile_id=active_profile)
    assert len(ranked) == 1
    assert ranked[0].assessment.id == current
    assert queue.total_assessed_jobs == 1
    assert not queue.mandatory_user_review
    assert queue.opportunities[0].profile_fit_score == 0.6


def test_timestamp_ties_use_a_stable_assessment_identity(
    container: Container, active_profile: str
) -> None:
    job_id = add_role(container)
    first = assess(container, active_profile, job_id, version="one")
    second = assess(container, active_profile, job_id, version="two")
    with container.database.session() as session:
        ranked = container.assessments.rank(session, profile_id=active_profile)
    assert len(ranked) == 1
    assert ranked[0].assessment.id == max(first, second)


@pytest.mark.parametrize("status", list(JobStatus))
def test_high_professional_fit_survives_penalties_limits_and_workflow_status(
    container: Container, active_profile: str, status: JobStatus
) -> None:
    job_id = add_role(container)
    assess(
        container,
        active_profile,
        job_id,
        fit=0.1,
        explanation={
            "profile_fit_score": 0.92,
            "eligibility": "currently_ineligible",
            "concern": "Employer explicitly excludes sponsorship.",
            "evidence": "No sponsorship is available for this vacancy.",
            "next_step": "Verify whether this restriction has changed.",
        },
    )
    second = add_role(container, "Engineer")
    assess(container, active_profile, second)
    with container.database.session() as session:
        session.get(Job, job_id).status = status
    with container.database.session() as session:
        before = {
            table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
        queue = container.opportunities.queue(session, profile_id=active_profile, limit=1)
        after = {
            table.name: session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
        assert session.get(Job, job_id).status == status
        assert container.audit.verify(session).valid
    assert before == after
    assert len(queue.mandatory_user_review) == 2
    card = next(card for card in queue.mandatory_user_review if card.job_id == job_id)
    assert card.profile_fit_score == 0.92
    assert card.priority_score == pytest.approx(0.92 * 0.9 * (1 - 0.35 * 0.5))
    assert card.eligibility_label == EligibilityLabel.CURRENTLY_INELIGIBLE
    assert card.unresolved_fact == "Employer explicitly excludes sponsorship."
    assert card.evidence == ("No sponsorship is available for this vacancy.",)
    assert card.next_verification_step.startswith("Non-actionable")


def test_recent_order_uses_discovery_date_without_claiming_employer_freshness(
    container: Container, active_profile: str
) -> None:
    old = add_role(container, "Old", days_ago=30)
    recent = add_role(container, "Recent")
    assess(container, active_profile, old, fit=0.74)
    assess(container, active_profile, recent, fit=0.6)
    with container.database.session() as session:
        queue = container.opportunities.queue(session, profile_id=active_profile, limit=1)
        priority = container.opportunities.queue(
            session, profile_id=active_profile, limit=1, order=OpportunityOrder.PRIORITY
        )
    assert queue.opportunities[0].job_id == recent
    assert queue.opportunities[0].posted_at is None
    assert queue.total_other_opportunities == 2
    assert priority.opportunities[0].job_id == old
    assert "not a live availability check" in queue.note


@pytest.mark.parametrize("label", ["confirmed", "conflicting", "currently_ineligible"])
def test_new_eligibility_conclusions_require_evidence(label: str) -> None:
    with pytest.raises(ValidationError, match="require recorded evidence"):
        ReviewEvidence.model_validate(
            {
                "eligibility_label": label,
                "canonical_url": "https://example.test/jobs/1",
                "next_verification_step": "Recheck before applying.",
                "unresolved_fact": "A material issue.",
            }
        )


@pytest.mark.parametrize("issue,forced", [("", False), ("Salary is unknown.", True)])
def test_confirmed_eligibility_does_not_hide_other_material_concerns(
    container: Container, active_profile: str, issue: str, forced: bool
) -> None:
    job_id = add_role(container)
    review = ReviewEvidence.model_validate(
        {
            "eligibility_label": "confirmed",
            "canonical_url": "https://employer.test/role",
            "evidence": [
                "Employer confirms sponsorship and required qualifications for this role."
            ],
            "unresolved_fact": issue,
            "next_verification_step": "Recheck the employer requirements before applying.",
        }
    )
    assess(container, active_profile, job_id, review=review)
    with container.database.session() as session:
        queue = container.opportunities.queue(session, profile_id=active_profile)
    assert bool(queue.mandatory_user_review) is forced
    card = (queue.mandatory_user_review or queue.opportunities)[0]
    assert card.canonical_url == "https://employer.test/role"
    assert card.eligibility_label == EligibilityLabel.CONFIRMED


@pytest.mark.parametrize(
    "location,expected",
    [
        ("London, UK", EligibilityLabel.UNKNOWN),
        ("Remote, US", EligibilityLabel.UNKNOWN),
        ("Paris, France", EligibilityLabel.UNKNOWN),
    ],
)
def test_geography_and_vacancy_cannot_establish_candidate_work_rights(
    container: Container, active_profile: str, location: str, expected: EligibilityLabel
) -> None:
    job_id = add_role(container)
    assess(container, active_profile, job_id, explanation={"sponsorship_status": "not_available"})
    with container.database.session() as session:
        session.get(Job, job_id).location = location
    with container.database.session() as session:
        card = container.opportunities.queue(
            session, profile_id=active_profile
        ).mandatory_user_review[0]
    assert card.eligibility_label == expected
    assert "not_available" in card.unresolved_fact


def test_malformed_historical_review_cannot_confirm_eligibility(
    container: Container, active_profile: str
) -> None:
    job_id = add_role(container)
    assessment_id = assess(container, active_profile, job_id)
    with container.database.session() as session:
        session.get(JobAssessment, assessment_id).explanation_json = {
            "review": {"eligibility_label": "confirmed"},
            "source_url": "javascript:alert(1)",
        }
    with container.database.session() as session:
        card = container.opportunities.queue(
            session, profile_id=active_profile
        ).mandatory_user_review[0]
    assert card.eligibility_label == EligibilityLabel.UNKNOWN
    assert "incomplete or invalid" in card.unresolved_fact
    assert card.canonical_url == "https://example.test/jobs/researcher"


def test_reassessment_replaces_obsolete_task_and_shortlisting_retires_review(
    container: Container, active_profile: str
) -> None:
    job_id = add_role(container)
    assess(container, active_profile, job_id)
    with container.database.session() as session:
        container.planner.reconcile(session, profile_id=active_profile, actor="test")
        old = session.scalar(select(Task).where(Task.title.startswith("Review role:")))
        old_id = old.id
    # Same scoring version and clock instant: changed evidence must still refresh the task.
    assess(
        container,
        active_profile,
        job_id,
        explanation={
            "eligibility": "currently_ineligible",
            "concern": "No sponsorship.",
            "next_step": "Check the restriction.",
        },
    )
    with container.database.session() as session:
        container.planner.reconcile(session, profile_id=active_profile, actor="test")
        container.planner.reconcile(session, profile_id=active_profile, actor="test")
        assert session.get(Task, old_id).status == TaskStatus.CANCELLED
        open_reviews = session.scalars(
            select(Task).where(
                Task.title.startswith("Review role:"), Task.status == TaskStatus.OPEN
            )
        ).all()
        assert len(open_reviews) == 1
        assert "Non-actionable" in open_reviews[0].description
        container.assessments.shortlist(session, job_id=job_id, actor="test")
        container.planner.reconcile(session, profile_id=active_profile, actor="test")
        assert open_reviews[0].status == TaskStatus.CANCELLED
        assert container.audit.verify(session).valid


def test_saved_brief_lookup_is_scoped_to_candidate(
    container: Container, active_profile: str
) -> None:
    first = container.daily_agent.run(profile_id=active_profile)
    with container.database.session() as session:
        other = CandidateProfile(name="Other", is_active=False)
        session.add(other)
        session.flush()
        other_id = other.id
    container.daily_agent.run(profile_id=other_id)
    with container.database.session() as session:
        view = container.briefs.latest(session, profile_id=active_profile)
    assert view is not None
    assert view.brief.id == first.view.brief.id


def test_programmatic_daily_agent_exposes_all_mandatory_evidence_beyond_task_limit(
    container: Container, active_profile: str
) -> None:
    for title in ["Researcher", "Engineer"]:
        assess(container, active_profile, add_role(container, title))
    result = container.daily_agent.run(profile_id=active_profile, limit=1)
    assert len(result.view.items) == 1
    assert len(result.view.opportunity_queue.mandatory_user_review) == 2
    with container.database.session() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.agent_name == "daily-ceo-agent"))
        assert len(run.output_json["mandatory_user_review"]) == 2
        assert all(card["canonical_url"] for card in run.output_json["mandatory_user_review"])


@pytest.mark.parametrize("label", ["unknown", "currently_ineligible"])
def test_structured_eligibility_keeps_other_recorded_concerns_visible(
    container: Container, active_profile: str, label: str
) -> None:
    job_id = add_role(container)
    review = ReviewEvidence.model_validate(
        {
            "eligibility_label": "confirmed",
            "canonical_url": "https://example.test/jobs/researcher",
            "evidence": ["Employer now offers sponsorship."],
            "next_verification_step": "Confirm the terms.",
        }
    )
    assess(
        container,
        active_profile,
        job_id,
        review=review,
        explanation={
            "gap": "Salary is still unknown.",
            "evidence": "No pay band in the employer advert.",
            "eligibility": label,
        },
    )
    with container.database.session() as session:
        card = container.opportunities.queue(
            session, profile_id=active_profile
        ).mandatory_user_review[0]
    assert "Salary is still unknown" in card.unresolved_fact
    assert "No pay band in the employer advert." in card.evidence
    if label == "currently_ineligible":
        assert card.eligibility_label == EligibilityLabel.CONFLICTING


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, "0.9", -0.1, 1.1])
def test_invalid_professional_fit_is_rejected_before_persistence(
    container: Container, active_profile: str, score: object
) -> None:
    job_id = add_role(container)
    with pytest.raises(ValueError, match="profile_fit_score"):
        assess(container, active_profile, job_id, explanation={"profile_fit_score": score})
    with container.database.session() as session:
        assert session.scalar(select(func.count(JobAssessment.id))) == 0


@pytest.mark.parametrize("days_until_due,overdue", [(0, True), (-1, True), (1, False)])
def test_daily_review_handles_sqlite_due_dates(
    container: Container, active_profile: str, days_until_due: int, overdue: bool
) -> None:
    add_role(container)
    with container.database.session() as session:
        session.add(
            Task(
                title="Review employer response",
                status=TaskStatus.OPEN,
                priority=100,
                due_at=container.clock.now() + timedelta(days=days_until_due),
                estimated_minutes=5,
            )
        )
    result = container.daily_agent.run(profile_id=active_profile, limit=1)
    item = result.view.items[0]
    assert item.recommendation == "Review employer response"
    assert ("due or overdue" in item.rationale) is overdue
    assert item.expected_roi == (1.0 if overdue else 0.9)
