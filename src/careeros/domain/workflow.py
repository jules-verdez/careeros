"""Reviewed inputs for the application and interview workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from careeros.domain.job_review import ReviewEvidence

Text = Annotated[str, Field(min_length=1)]


def utc(value: datetime) -> datetime:
    """Stored SQLite timestamps are UTC even when their timezone was stripped."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Include a timezone offset, for example 2026-10-01T09:00:00+01:00")
    return utc(parsed)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class CandidateFact(InputModel):
    key: Text
    kind: Literal["experience", "project", "achievement", "skill", "education", "answer"]
    text: Text
    source: Text
    tags: tuple[Text, ...] = ()


class CandidateSnapshot(InputModel):
    name: Text
    headline: Text
    facts: tuple[CandidateFact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len({fact.key for fact in self.facts}) != len(self.facts):
            raise ValueError("Candidate fact keys must be unique")
        return self


class PacketAnswer(InputModel):
    question: Text
    evidence_keys: tuple[Text, ...] = Field(min_length=1)


class PacketSpec(InputModel):
    evidence_keys: tuple[Text, ...] = Field(min_length=1)
    answers: tuple[PacketAnswer, ...] = ()


class VacancyStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"


class VacancyEvidence(InputModel):
    source_url: HttpUrl
    status: VacancyStatus
    evidence: tuple[Text, ...] = Field(min_length=1)
    checked_at: datetime
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    location: str | None = None
    sponsorship_evidence: tuple[Text, ...] = ()
    review: ReviewEvidence | None = None

    @model_validator(mode="after")
    def explicit_timezones(self) -> Self:
        for value in (self.checked_at, self.published_at, self.deadline_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("Verification timestamps must include timezone offsets")
        if self.review and str(self.review.canonical_url) != str(self.source_url):
            raise ValueError("Eligibility review must cite the same verified vacancy URL")
        return self


class InterviewPlan(InputModel):
    format: Text
    interviewer: str = ""
    logistics: Text
    evidence_keys: tuple[Text, ...] = Field(min_length=1)
    practice_questions: tuple[Text, ...] = ()
    questions_to_ask: tuple[Text, ...] = ()


class InterviewDebrief(InputModel):
    questions_asked: tuple[Text, ...] = ()
    employer_feedback: str = ""
    reflection: Text
    next_step: Text
