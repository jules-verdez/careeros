"""Human- or model-supplied job assessments and shortlist ranking."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import JobStatus
from careeros.database.models import CandidateProfile, Company, Job, JobAssessment
from careeros.domain.job_review import ReviewEvidence
from careeros.services.audit import AuditService


@dataclass(frozen=True)
class RankedJob:
    job: Job
    assessment: JobAssessment
    company: Company
    priority_score: float


class AssessmentService:
    """Persist assessments without pretending the inputs are calibrated probabilities."""

    SCORING_VERSION = "manual-v1"
    COMPETITION_PENALTY = 0.35
    #: Statuses an operator may move between while triaging. Applied and closed roles
    #: belong to the application workflow, which records the evidence for them.
    TRIAGE_STATUSES = frozenset({JobStatus.DISCOVERED, JobStatus.SHORTLISTED, JobStatus.DISMISSED})

    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self._clock = clock
        self._audit = audit

    def assess(
        self,
        session: Session,
        *,
        job_id: str,
        profile_id: str,
        fit_score: float,
        confidence: float,
        competition_score: float,
        explanation: dict[str, Any],
        actor: str,
        scoring_version: str = SCORING_VERSION,
        review: ReviewEvidence | None = None,
    ) -> JobAssessment:
        self._validate_score("fit_score", fit_score)
        self._validate_score("confidence", confidence)
        self._validate_score("competition_score", competition_score)
        explanation = dict(explanation)
        professional_fit = explanation.get("profile_fit_score", fit_score)
        self._validate_score("profile_fit_score", professional_fit)
        explanation["profile_fit_score"] = professional_fit
        if review is not None:
            explanation["review"] = review.model_dump(mode="json")
        elif "review" in explanation:
            explanation["review"] = ReviewEvidence.model_validate(explanation["review"]).model_dump(
                mode="json"
            )
        if session.get(Job, job_id) is None:
            raise LookupError(f"job not found: {job_id}")
        if session.get(CandidateProfile, profile_id) is None:
            raise LookupError(f"candidate profile not found: {profile_id}")
        assessment = session.scalar(
            select(JobAssessment).where(
                JobAssessment.job_id == job_id,
                JobAssessment.candidate_profile_id == profile_id,
                JobAssessment.scoring_version == scoring_version,
            )
        )
        if assessment is None:
            assessment = JobAssessment(
                job_id=job_id,
                candidate_profile_id=profile_id,
                scoring_version=scoring_version,
                fit_score=fit_score,
                confidence=confidence,
                competition_score=competition_score,
                explanation_json=explanation,
                assessed_at=self._clock.now(),
            )
            session.add(assessment)
            session.flush()
            event_type = "job.assessed"
        else:
            assessment.fit_score = fit_score
            assessment.confidence = confidence
            assessment.competition_score = competition_score
            assessment.explanation_json = explanation
            assessment.assessed_at = self._clock.now()
            event_type = "job.reassessed"
        self._audit.append(
            session,
            actor=actor,
            event_type=event_type,
            aggregate_type="job",
            aggregate_id=job_id,
            payload={
                "assessment_id": assessment.id,
                "scoring_version": scoring_version,
                "fit_score": fit_score,
                "profile_fit_score": professional_fit,
                "confidence": confidence,
                "competition_score": competition_score,
                "review": explanation.get("review"),
            },
        )
        return assessment

    @classmethod
    def latest(
        cls,
        session: Session,
        *,
        profile_id: str,
        job_ids: set[str] | None = None,
    ) -> list[RankedJob]:
        """One current assessment per job/profile, with deterministic timestamp ties."""

        scope = JobAssessment.candidate_profile_id == profile_id
        if job_ids is not None:
            scope = scope & JobAssessment.job_id.in_(job_ids)
        current = (
            select(
                JobAssessment.id.label("assessment_id"),
                func.row_number()
                .over(
                    partition_by=JobAssessment.job_id,
                    order_by=(JobAssessment.assessed_at.desc(), JobAssessment.id.desc()),
                )
                .label("position"),
            )
            .where(scope)
            .subquery()
        )
        rows = session.execute(
            select(Job, JobAssessment, Company)
            .join(JobAssessment, JobAssessment.job_id == Job.id)
            .join(Company, Company.id == Job.company_id)
            .join(current, current.c.assessment_id == JobAssessment.id)
            .where(current.c.position == 1)
        ).all()
        ranked = [
            RankedJob(
                job=job,
                assessment=assessment,
                company=company,
                priority_score=cls.priority_score(assessment),
            )
            for job, assessment, company in rows
        ]
        ranked.sort(key=lambda item: (-item.priority_score, item.job.id))
        return ranked

    @classmethod
    def rank(
        cls,
        session: Session,
        *,
        profile_id: str,
        limit: int = 20,
    ) -> list[RankedJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return [
            item
            for item in cls.latest(session, profile_id=profile_id)
            if item.job.status in {JobStatus.DISCOVERED, JobStatus.SHORTLISTED}
        ][:limit]

    def shortlist(self, session: Session, *, job_id: str, actor: str) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"job not found: {job_id}")
        job.status = JobStatus.SHORTLISTED
        self._audit.append(
            session,
            actor=actor,
            event_type="job.shortlisted",
            aggregate_type="job",
            aggregate_id=job.id,
            payload={},
        )
        return job

    def triage_job(self, session: Session, job_id: str) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"job not found: {job_id}")
        if job.status not in self.TRIAGE_STATUSES:
            raise ValueError(
                f"This role is {job.status.value}; its status follows the application workflow."
            )
        return job

    def dismiss(self, session: Session, *, job_id: str, actor: str, reason: str = "") -> Job:
        """Hide a role from the ordinary queue. High-fit roles stay in mandatory review."""
        job = self.triage_job(session, job_id)
        job.status = JobStatus.DISMISSED
        self._audit.append(
            session,
            actor=actor,
            event_type="job.dismissed",
            aggregate_type="job",
            aggregate_id=job.id,
            payload={"reason": reason.strip()},
        )
        return job

    def restore(self, session: Session, *, job_id: str, actor: str) -> Job:
        job = self.triage_job(session, job_id)
        job.status = JobStatus.DISCOVERED
        self._audit.append(
            session,
            actor=actor,
            event_type="job.restored",
            aggregate_type="job",
            aggregate_id=job.id,
            payload={},
        )
        return job

    @classmethod
    def priority_score(cls, assessment: JobAssessment) -> float:
        return (
            cls.profile_fit_score(assessment)
            * assessment.confidence
            * (1.0 - cls.COMPETITION_PENALTY * assessment.competition_score)
        )

    @staticmethod
    def profile_fit_score(assessment: JobAssessment) -> float:
        """Prefer the explicit, pre-eligibility score in historical research records."""

        value = assessment.explanation_json.get("profile_fit_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            return float(value)
        return assessment.fit_score

    @staticmethod
    def _validate_score(name: str, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"{name} must be in [0, 1]")
