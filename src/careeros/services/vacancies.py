"""Persist read-only vacancy checks without inferring eligibility from availability."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import ConnectorRunStatus
from careeros.database.models import CandidateProfile, ConnectorRun, Job, VacancyVerification
from careeros.domain.workflow import VacancyEvidence, VacancyStatus, utc
from careeros.services.assessment import AssessmentService
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload


class VacancyService:
    MAX_AGE = timedelta(hours=48)

    def __init__(self, clock: Clock, audit: AuditService, assessments: AssessmentService) -> None:
        self.clock = clock
        self.audit = audit
        self.assessments = assessments

    def record(
        self,
        session: Session,
        *,
        job_id: str,
        profile_id: str,
        evidence: VacancyEvidence,
        actor: str,
        connector_name: str = "human-review",
        reviewed_by: str | None = None,
    ) -> VacancyVerification:
        if not session.get(Job, job_id) or not session.get(CandidateProfile, profile_id):
            raise LookupError("Job or candidate profile not found")
        if utc(evidence.checked_at) > self.clock.now():
            raise ValueError("A verification cannot be dated in the future")
        if connector_name == "human-review" and not (reviewed_by or "").strip():
            raise ValueError("Human verification requires a named reviewer")
        if evidence.review and not (reviewed_by or "").strip():
            raise ValueError("Eligibility conclusions require a named human reviewer")
        payload = evidence.model_dump(mode="json")
        previous_check = self.latest(session, job_id, profile_id)
        if (
            evidence.review
            and previous_check
            and utc(evidence.checked_at) < utc(previous_check.checked_at)
        ):
            raise ValueError("An older eligibility review cannot replace a newer vacancy check")
        verification = VacancyVerification(
            job_id=job_id,
            candidate_profile_id=profile_id,
            connector_name=connector_name,
            source_url=str(evidence.source_url),
            status=evidence.status.value,
            revision=(
                session.scalar(
                    select(func.max(VacancyVerification.revision)).where(
                        VacancyVerification.job_id == job_id,
                        VacancyVerification.candidate_profile_id == profile_id,
                    )
                )
                or 0
            )
            + 1,
            checked_at=utc(evidence.checked_at),
            recorded_at=self.clock.now(),
            evidence_json=payload,
            sha256=hash_payload(payload),
            reviewed_by=reviewed_by,
        )
        session.add(verification)
        session.flush()
        if evidence.review:
            rows = self.assessments.latest(session, profile_id=profile_id)
            previous = next((row.assessment for row in rows if row.job.id == job_id), None)
            if previous is None:
                raise ValueError(
                    "Assess professional fit before recording an eligibility conclusion"
                )
            self.assessments.assess(
                session,
                job_id=job_id,
                profile_id=profile_id,
                fit_score=previous.fit_score,
                confidence=previous.confidence,
                competition_score=previous.competition_score,
                explanation=previous.explanation_json,
                actor=actor,
                scoring_version=previous.scoring_version,
                review=evidence.review,
            )
        if connector_name != "human-review":
            session.add(
                ConnectorRun(
                    connector_name=connector_name,
                    operation="verify_vacancy",
                    status=(
                        ConnectorRunStatus.FAILED
                        if evidence.status == VacancyStatus.UNREACHABLE
                        else ConnectorRunStatus.SUCCEEDED
                    ),
                    request_json={"job_id": job_id, "source_url": str(evidence.source_url)},
                    response_summary_json={
                        "verification_id": verification.id,
                        "status": evidence.status.value,
                    },
                    started_at=evidence.checked_at,
                    finished_at=self.clock.now(),
                    error="; ".join(evidence.evidence)
                    if evidence.status == VacancyStatus.UNREACHABLE
                    else None,
                )
            )
        self.audit.append(
            session,
            actor=actor,
            event_type="vacancy.verified",
            aggregate_type="job",
            aggregate_id=job_id,
            payload={
                "verification_id": verification.id,
                "status": verification.status,
                "sha256": verification.sha256,
            },
        )
        return verification

    @staticmethod
    def latest(session: Session, job_id: str, profile_id: str) -> VacancyVerification | None:
        return session.scalar(
            select(VacancyVerification)
            .where(
                VacancyVerification.job_id == job_id,
                VacancyVerification.candidate_profile_id == profile_id,
            )
            .order_by(
                VacancyVerification.checked_at.desc(),
                VacancyVerification.revision.desc(),
            )
            .limit(1)
        )

    def current(self, verification: VacancyVerification) -> bool:
        age = self.clock.now() - utc(verification.checked_at)
        return timedelta(0) <= age <= self.MAX_AGE
