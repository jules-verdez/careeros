"""Read-only opportunity views with complete high-fit human-review disclosure."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import JobStatus, SourceKind
from careeros.database.models import JobObservation, Source
from careeros.domain.job_review import (
    EligibilityLabel,
    ReviewEvidence,
    evidence_items,
    legacy_eligibility,
    text_value,
)
from careeros.services.assessment import AssessmentService, RankedJob


class OpportunityOrder(StrEnum):
    RECENT = "recent"
    PRIORITY = "priority"


class Opportunity(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    assessment_id: str
    title: str
    company: str
    location: str | None
    canonical_url: str | None
    job_status: JobStatus
    profile_fit_score: float
    fit_basis: str
    confidence: float
    priority_score: float
    eligibility_label: EligibilityLabel
    unresolved_fact: str
    evidence: tuple[str, ...]
    next_verification_step: str
    mandatory_user_review: bool
    assessed_at: datetime
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime


class OpportunityQueue(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    order: OpportunityOrder
    total_assessed_jobs: int
    total_other_opportunities: int
    opportunities: tuple[Opportunity, ...]
    mandatory_user_review: tuple[Opportunity, ...]
    note: str = (
        "Stored research, not a live availability check. Fit and priority are ordering heuristics, "
        "not hiring probabilities. Recent means first discovered in CareerOS. The limit applies "
        "only to other opportunities; mandatory review is complete and grants no action approval."
    )


class OpportunityService:
    HIGH_FIT_THRESHOLD = 0.75
    _EMPLOYER_SOURCES = frozenset(
        {
            SourceKind.GREENHOUSE,
            SourceKind.LEVER,
            SourceKind.ASHBY,
            SourceKind.SMARTRECRUITERS,
            SourceKind.WORKDAY,
            SourceKind.CAREER_PAGE,
            SourceKind.COMPANY_WEBSITE,
        }
    )

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def queue(
        self,
        session: Session,
        *,
        profile_id: str,
        limit: int = 20,
        order: OpportunityOrder = OpportunityOrder.RECENT,
    ) -> OpportunityQueue:
        if limit < 1:
            raise ValueError("limit must be positive")
        ranked = AssessmentService.latest(session, profile_id=profile_id)
        urls = self.source_urls(session, {row.job.id for row in ranked})
        cards = [self.describe(row, fallback_url=urls.get(row.job.id)) for row in ranked]
        if order == OpportunityOrder.RECENT:
            cards.sort(
                key=lambda card: (
                    -card.first_seen_at.timestamp(),
                    -card.priority_score,
                    card.job_id,
                )
            )
        else:
            cards.sort(
                key=lambda card: (
                    -card.priority_score,
                    -card.first_seen_at.timestamp(),
                    card.job_id,
                )
            )
        mandatory = tuple(card for card in cards if card.mandatory_user_review)
        other = [
            card
            for card in cards
            if not card.mandatory_user_review
            and card.job_status in {JobStatus.DISCOVERED, JobStatus.SHORTLISTED}
            and card.eligibility_label != EligibilityLabel.CURRENTLY_INELIGIBLE
        ]
        return OpportunityQueue(
            generated_at=self._clock.now(),
            order=order,
            total_assessed_jobs=len(cards),
            total_other_opportunities=len(other),
            opportunities=tuple(other[:limit]),
            mandatory_user_review=mandatory,
        )

    def card(self, session: Session, *, profile_id: str, job_id: str) -> Opportunity | None:
        """The review card for one role, built exactly as the queue builds it."""
        rows = AssessmentService.latest(session, profile_id=profile_id, job_ids={job_id})
        if not rows:
            return None
        return self.describe(rows[0], fallback_url=self.source_urls(session, {job_id}).get(job_id))

    @classmethod
    def describe(cls, row: RankedJob, *, fallback_url: str | None = None) -> Opportunity:
        job, assessment = row.job, row.assessment
        data = assessment.explanation_json
        issues: list[str] = []
        url: str | None
        legacy_fact = text_value(
            data,
            "unresolved_fact",
            "unresolved_adverse_fact",
            "concern",
            "gap",
            "eligibility_reason",
            "review_reason",
        )
        structured: ReviewEvidence | None = None
        if "review" in data:
            try:
                structured = ReviewEvidence.model_validate(data["review"])
            except ValidationError:
                issues.append("Stored review is incomplete or invalid; re-record the evidence.")
        if structured is not None:
            label = structured.eligibility_label
            fact = structured.unresolved_fact
            evidence = structured.evidence
            next_step = structured.next_verification_step
            url = str(structured.canonical_url)
            if legacy_fact and legacy_fact != fact:
                issues.append(legacy_fact)
                evidence = tuple(dict.fromkeys((*evidence, *evidence_items(data.get("evidence")))))
            if data.get("mandatory_user_review") is True:
                issues.append("The recorded assessment explicitly requests human review.")
            legacy_label = legacy_eligibility(data, location=job.location)
            if label == EligibilityLabel.CONFIRMED and legacy_label in {
                EligibilityLabel.CURRENTLY_INELIGIBLE,
                EligibilityLabel.CONFLICTING,
            }:
                label = EligibilityLabel.CONFLICTING
                issues.append(
                    "The structured eligibility review conflicts with the legacy eligibility label."
                )
        else:
            label = legacy_eligibility(data, location=job.location)
            fact = legacy_fact
            evidence = (
                evidence_items(data.get("evidence"))
                or evidence_items(data.get("eligibility_evidence"))
                or evidence_items(data.get("sponsorship_evidence"))
            )
            next_step = text_value(data, "next_verification_step", "next_step", "next")
            url = cls._url(text_value(data, "canonical_url", "source_url")) or fallback_url
            sponsorship = text_value(data, "sponsorship_status")
            if sponsorship:
                issues.append(f"Recorded vacancy sponsorship status: {sponsorship}.")
            if data.get("mandatory_user_review") is True and not fact:
                issues.append("The recorded assessment explicitly requests human review.")
        if fact:
            issues.insert(0, fact)
        if label == EligibilityLabel.UNKNOWN:
            issues.append(
                "Vacancy-specific eligibility is unconfirmed. Review the candidate's recorded work "
                "rights and the employer's requirements before determining eligibility."
            )
        elif label != EligibilityLabel.CONFIRMED and not fact:
            issues.append(f"The recorded eligibility label is {label.value}; verify its basis.")
        if url is None:
            issues.append("The canonical vacancy URL is missing from the saved evidence.")
        if not evidence:
            evidence = (
                f"No supporting eligibility evidence recorded in assessment {assessment.id}.",
            )
        if not next_step:
            next_step = (
                "Verify the employer-authored vacancy, candidate work rights, sponsorship, "
                "and material requirements; record the source and exact evidence."
            )
        if label == EligibilityLabel.CURRENTLY_INELIGIBLE:
            next_step = (
                "Non-actionable under the recorded requirements. Verify whether the adverse "
                "requirement or candidate circumstances have changed. " + next_step
            )
        fit = AssessmentService.profile_fit_score(assessment)
        return Opportunity(
            job_id=job.id,
            assessment_id=assessment.id,
            title=job.title,
            company=row.company.canonical_name,
            location=job.location,
            canonical_url=url,
            job_status=job.status,
            profile_fit_score=fit,
            fit_basis=(
                "explicit professional fit"
                if data.get("profile_fit_score") == fit
                else "legacy fit_score; pre-eligibility basis not recorded"
            ),
            confidence=assessment.confidence,
            priority_score=row.priority_score,
            eligibility_label=label,
            unresolved_fact=" ".join(dict.fromkeys(issues)),
            evidence=evidence,
            next_verification_step=next_step,
            mandatory_user_review=(
                fit > cls.HIGH_FIT_THRESHOLD
                and (label != EligibilityLabel.CONFIRMED or bool(issues))
            ),
            assessed_at=cls._utc(assessment.assessed_at),
            posted_at=cls._utc(job.posted_at) if job.posted_at else None,
            first_seen_at=cls._utc(job.first_seen_at),
            last_seen_at=cls._utc(job.last_seen_at),
        )

    @classmethod
    def source_urls(cls, session: Session, job_ids: set[str]) -> dict[str, str]:
        """Each job's best source URL, preferring the employer's own board."""
        if not job_ids:
            return {}
        # Fetch source observations in one batch instead of issuing a query per card.
        rows = session.execute(
            select(JobObservation, Source.kind)
            .join(Source, Source.id == JobObservation.source_id)
            .where(JobObservation.job_id.in_(job_ids))
            .order_by(JobObservation.observed_at.desc(), JobObservation.id.desc())
        ).all()
        rows = sorted(rows, key=lambda row: row[1] not in cls._EMPLOYER_SOURCES)
        urls: dict[str, str] = {}
        for observation, _kind in rows:
            url = cls._url(observation.source_url)
            if url and observation.job_id not in urls:
                urls[observation.job_id] = url
        return urls

    @staticmethod
    def _url(value: str) -> str | None:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        return value if parsed.scheme in {"http", "https"} and parsed.hostname else None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
