"""Normalized relational model for CareerOS.

The database stores facts and workflow state. Provider payloads are retained as
documents/observations with provenance; they are not copied into multiple domain
tables. All externally visible mutations are represented by ``ActionRequest``
before a connector may execute them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from careeros.database.base import Base, IdMixin, TimestampMixin, new_id, utc_now
from careeros.database.enums import (
    ActionKind,
    ActionStatus,
    AgentRunStatus,
    ApplicationStatus,
    AssistantStatus,
    ConnectorRunStatus,
    ContactDeliveryStatus,
    ContactEnrichmentReason,
    DeliveryEventKind,
    Direction,
    DocumentKind,
    EmailStatus,
    EmploymentType,
    InteractionKind,
    JobStatus,
    MeetingStatus,
    NoteKind,
    PreferenceKind,
    RecipientKind,
    RequirementKind,
    SourceKind,
    SponsorMatchStatus,
    TaskStatus,
    ToolCallStatus,
    ToolRisk,
)


def enum_column(enum_type: type[Any], name: str) -> Enum:
    return Enum(enum_type, name=name, native_enum=False, validate_strings=True)


class Company(Base, IdMixin, TimestampMixin):
    __tablename__ = "companies"

    canonical_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True)
    website_url: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    headquarters: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (Index("ix_companies_canonical_name", "canonical_name"),)


class CompanyAlias(Base, IdMixin):
    __tablename__ = "company_aliases"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(255))
    normalized_alias: Mapped[str] = mapped_column(String(255), unique=True)


class CompanySponsorshipAssessment(Base):
    __tablename__ = "company_sponsorship_assessments"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[SponsorMatchStatus] = mapped_column(
        enum_column(SponsorMatchStatus, "sponsor_match_status"), index=True
    )
    matched_organisation: Mapped[str | None] = mapped_column(String(500))
    candidate_organisations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    match_basis: Mapped[str] = mapped_column(String(64))
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_url: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        CheckConstraint(
            "match_score >= 0 AND match_score <= 1",
            name="valid_sponsor_match_score",
        ),
    )


class Source(Base, IdMixin, TimestampMixin):
    __tablename__ = "sources"

    kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind, "source_kind"))
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str | None] = mapped_column(Text)
    connector_name: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_sources_kind_name"),
        Index("ix_sources_connector_name", "connector_name"),
    )


class Document(Base, IdMixin, TimestampMixin):
    __tablename__ = "documents"

    kind: Mapped[DocumentKind] = mapped_column(enum_column(DocumentKind, "document_kind"))
    title: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(127))
    content_text: Mapped[str | None] = mapped_column(Text)
    content_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "content_text IS NULL OR content_json IS NULL",
            name="single_content_representation",
        ),
        Index("ix_documents_kind_created_at", "kind", "created_at"),
    )


class DocumentSource(Base):
    __tablename__ = "document_sources"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    locator: Mapped[str | None] = mapped_column(Text)


class Person(Base, IdMixin, TimestampMixin):
    __tablename__ = "people"

    full_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    location: Mapped[str | None] = mapped_column(String(255))
    canonical_profile_url: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        UniqueConstraint(
            "normalized_name",
            "canonical_profile_url",
            name="uq_people_name_profile",
        ),
    )


class PersonEmployment(Base, IdMixin, TimestampMixin):
    __tablename__ = "person_employments"

    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    normalized_title: Mapped[str] = mapped_column(String(255), index=True)
    location: Mapped[str | None] = mapped_column(String(255))
    started_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    hiring_influence_probability: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        CheckConstraint(
            "hiring_influence_probability IS NULL OR "
            "(hiring_influence_probability >= 0 AND hiring_influence_probability <= 1)",
            name="valid_influence_probability",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="valid_employment_confidence",
        ),
        Index(
            "ix_person_employments_company_current",
            "company_id",
            "is_current",
        ),
    )


class PersonContact(Base, IdMixin, TimestampMixin):
    __tablename__ = "person_contacts"

    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    contact_type: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    public_source_url: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[ContactDeliveryStatus] = mapped_column(
        enum_column(ContactDeliveryStatus, "contact_delivery_status"),
        default=ContactDeliveryStatus.UNKNOWN,
        index=True,
    )
    delivery_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "person_id",
            "contact_type",
            "normalized_value",
            name="uq_person_contacts_identity",
        ),
        CheckConstraint(
            "contact_type != 'email' OR (is_public = 1 AND public_source_url IS NOT NULL)",
            name="public_email_requires_source",
        ),
    )


class ContactEnrichmentObservation(Base, IdMixin):
    __tablename__ = "contact_enrichment_observations"

    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), index=True)
    fallback_reason: Mapped[ContactEnrichmentReason] = mapped_column(
        enum_column(ContactEnrichmentReason, "contact_enrichment_reason")
    )
    email_address: Mapped[str | None] = mapped_column(String(320), index=True)
    provider_status: Mapped[str] = mapped_column(String(64))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    credits_spent: Mapped[int] = mapped_column(Integer, default=0)
    free_lookup: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(Text)
    response_sha256: Mapped[str] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        CheckConstraint("credits_spent >= 0", name="nonnegative_enrichment_credits"),
        UniqueConstraint(
            "person_id",
            "provider",
            "response_sha256",
            name="uq_contact_enrichment_observation",
        ),
    )


class Recruiter(Base):
    __tablename__ = "recruiters"

    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    agency_company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    specialties_json: Mapped[list[str]] = mapped_column(JSON, default=list)


class CandidateProfile(Base, IdMixin, TimestampMixin):
    __tablename__ = "candidate_profiles"

    name: Mapped[str] = mapped_column(String(255))
    headline: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(255))
    primary_email: Mapped[str | None] = mapped_column(String(320))
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    github_url: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    cv_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CandidatePreference(Base, IdMixin, TimestampMixin):
    __tablename__ = "candidate_preferences"

    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[PreferenceKind] = mapped_column(enum_column(PreferenceKind, "preference_kind"))
    value: Mapped[str] = mapped_column(String(500))
    normalized_value: Mapped[str] = mapped_column(String(500))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id",
            "kind",
            "normalized_value",
            name="uq_candidate_preferences_identity",
        ),
        CheckConstraint("weight >= 0", name="nonnegative_preference_weight"),
    )


class CandidateSkill(Base, IdMixin, TimestampMixin):
    __tablename__ = "candidate_skills"

    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    proficiency: Mapped[float | None] = mapped_column(Float)
    years_experience: Mapped[float | None] = mapped_column(Float)
    evidence_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id",
            "normalized_name",
            name="uq_candidate_skills_identity",
        ),
        CheckConstraint(
            "proficiency IS NULL OR (proficiency >= 0 AND proficiency <= 1)",
            name="valid_skill_proficiency",
        ),
        CheckConstraint(
            "years_experience IS NULL OR years_experience >= 0",
            name="nonnegative_years_experience",
        ),
    )


class Job(Base, IdMixin, TimestampMixin):
    __tablename__ = "jobs"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    canonical_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    location: Mapped[str | None] = mapped_column(String(255), index=True)
    remote_policy: Mapped[str | None] = mapped_column(String(64))
    employment_type: Mapped[EmploymentType | None] = mapped_column(
        enum_column(EmploymentType, "employment_type")
    )
    compensation_text: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status"),
        default=JobStatus.DISCOVERED,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_jobs_status_last_seen", "status", "last_seen_at"),
        Index("ix_jobs_company_title_location", "company_id", "normalized_title", "location"),
    )


class JobObservation(Base, IdMixin):
    __tablename__ = "job_observations"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    source_confidence: Mapped[float] = mapped_column(Float)
    content_sha256: Mapped[str] = mapped_column(String(64))
    raw_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_job_observations_external"),
        CheckConstraint(
            "source_confidence >= 0 AND source_confidence <= 1",
            name="valid_source_confidence",
        ),
        Index("ix_job_observations_job_observed", "job_id", "observed_at"),
    )


class JobRequirement(Base, IdMixin):
    __tablename__ = "job_requirements"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[RequirementKind] = mapped_column(enum_column(RequirementKind, "requirement_kind"))
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "kind",
            "normalized_text",
            name="uq_job_requirements_identity",
        ),
        CheckConstraint("weight >= 0", name="nonnegative_requirement_weight"),
    )


class JobAssessment(Base, IdMixin):
    __tablename__ = "job_assessments"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    scoring_version: Mapped[str] = mapped_column(String(64))
    fit_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    competition_score: Mapped[float] = mapped_column(Float)
    explanation_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "candidate_profile_id",
            "scoring_version",
            name="uq_job_assessments_version",
        ),
        CheckConstraint("fit_score >= 0 AND fit_score <= 1", name="valid_fit_score"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="valid_assessment_confidence",
        ),
        CheckConstraint(
            "competition_score >= 0 AND competition_score <= 1",
            name="valid_competition_score",
        ),
        Index("ix_job_assessments_rank", "fit_score", "confidence", "competition_score"),
    )


class CompanyDocument(Base):
    __tablename__ = "company_documents"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(64), primary_key=True)


class PersonDocument(Base):
    __tablename__ = "person_documents"

    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(64), primary_key=True)


class JobDocument(Base):
    __tablename__ = "job_documents"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(64), primary_key=True)


class ResearchReport(Base, IdMixin):
    __tablename__ = "research_reports"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date)
    markdown_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )
    json_document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    confidence: Mapped[float] = mapped_column(Float)
    generated_by_agent_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    __table_args__ = (
        UniqueConstraint("company_id", "as_of_date", name="uq_research_reports_snapshot"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="valid_research_confidence",
        ),
    )


class Application(Base, IdMixin, TimestampMixin):
    __tablename__ = "applications"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus, "application_status"),
        default=ApplicationStatus.DRAFT,
        index=True,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ApplicationDocument(Base):
    __tablename__ = "application_documents"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(64), primary_key=True)


class CandidateVersion(Base, IdMixin):
    __tablename__ = "candidate_versions"

    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    reviewed_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "version", name="uq_candidate_version_number"),
        CheckConstraint("version > 0", name="positive_candidate_version"),
    )


class ApplicationWorkspace(Base):
    __tablename__ = "application_workspaces"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="RESTRICT"), index=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role_family: Mapped[str] = mapped_column(String(255), default="unclassified")
    source_label: Mapped[str] = mapped_column(String(255), default="unclassified")


class ApplicationEvent(Base, IdMixin):
    __tablename__ = "application_events"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("application_id", "position", name="uq_application_event_position"),
    )


class VacancyVerification(Base, IdMixin):
    __tablename__ = "vacancy_verifications"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="RESTRICT"), index=True
    )
    connector_name: Mapped[str] = mapped_column(String(128))
    source_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint(
            "job_id", "candidate_profile_id", "revision", name="uq_vacancy_verification_revision"
        ),
    )


class ApplicationPacket(Base, IdMixin):
    __tablename__ = "application_packets"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    candidate_version_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_versions.id", ondelete="RESTRICT")
    )
    verification_id: Mapped[str | None] = mapped_column(
        ForeignKey("vacancy_verifications.id", ondelete="RESTRICT")
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("application_id", "sha256", name="uq_application_packet_content"),
    )


class ApplicationSubmission(Base, IdMixin):
    __tablename__ = "application_submissions"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), unique=True
    )
    packet_id: Mapped[str | None] = mapped_column(
        ForeignKey("application_packets.id", ondelete="RESTRICT")
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[str] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InterviewWorkspace(Base):
    __tablename__ = "interview_workspaces"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    candidate_version_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_versions.id", ondelete="RESTRICT")
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    preparation_markdown: Mapped[str] = mapped_column(Text)
    debrief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Email(Base, IdMixin, TimestampMixin):
    __tablename__ = "emails"

    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    person_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    connector_name: Mapped[str | None] = mapped_column(String(128))
    external_message_id: Mapped[str | None] = mapped_column(String(500))
    external_thread_id: Mapped[str | None] = mapped_column(String(500), index=True)
    rfc822_message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    direction: Mapped[Direction] = mapped_column(enum_column(Direction, "email_direction"))
    status: Mapped[EmailStatus] = mapped_column(
        enum_column(EmailStatus, "email_status"), index=True
    )
    sender_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998))
    body_document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    in_reply_to_email_id: Mapped[str | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL")
    )
    sent_or_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "connector_name",
            "external_message_id",
            name="uq_emails_external_message",
        ),
        Index("ix_emails_status_created_at", "status", "created_at"),
    )


class EmailRecipient(Base):
    __tablename__ = "email_recipients"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[RecipientKind] = mapped_column(
        enum_column(RecipientKind, "recipient_kind"), primary_key=True
    )
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    person_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )


class EmailParagraphRationale(Base, IdMixin):
    __tablename__ = "email_paragraph_rationales"

    email_id: Mapped[str] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), index=True)
    paragraph_index: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "email_id",
            "paragraph_index",
            name="uq_email_paragraph_rationales_index",
        ),
        CheckConstraint("paragraph_index >= 0", name="nonnegative_paragraph_index"),
    )


class EmailEvidence(Base):
    __tablename__ = "email_evidence"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True
    )


class EmailDeliveryEvent(Base, IdMixin):
    __tablename__ = "email_delivery_events"

    outbound_email_id: Mapped[str | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), index=True
    )
    notification_email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), unique=True
    )
    kind: Mapped[DeliveryEventKind] = mapped_column(
        enum_column(DeliveryEventKind, "delivery_event_kind"), index=True
    )
    recipient_address: Mapped[str | None] = mapped_column(String(320), index=True)
    smtp_status: Mapped[str | None] = mapped_column(String(32))
    diagnostic: Mapped[str | None] = mapped_column(Text)
    matched_by: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Meeting(Base, IdMixin, TimestampMixin):
    __tablename__ = "meetings"

    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    connector_name: Mapped[str | None] = mapped_column(String(128))
    external_event_id: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MeetingStatus] = mapped_column(
        enum_column(MeetingStatus, "meeting_status"), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "connector_name",
            "external_event_id",
            name="uq_meetings_external_event",
        ),
        CheckConstraint("ends_at > starts_at", name="meeting_positive_duration"),
    )


class MeetingAttendee(Base):
    __tablename__ = "meeting_attendees"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    email_address: Mapped[str] = mapped_column(String(320), primary_key=True)
    person_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    response_status: Mapped[str | None] = mapped_column(String(32))


class Note(Base, IdMixin, TimestampMixin):
    __tablename__ = "notes"

    kind: Mapped[NoteKind] = mapped_column(enum_column(NoteKind, "note_kind"))
    title: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)


class CompanyNote(Base):
    __tablename__ = "company_notes"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    note_id: Mapped[str] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )


class PersonNote(Base):
    __tablename__ = "person_notes"

    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    note_id: Mapped[str] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )


class JobNote(Base):
    __tablename__ = "job_notes"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    note_id: Mapped[str] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )


class ApplicationNote(Base):
    __tablename__ = "application_notes"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    note_id: Mapped[str] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )


class Task(Base, IdMixin, TimestampMixin):
    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        enum_column(TaskStatus, "task_status"),
        default=TaskStatus.OPEN,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    source_agent: Mapped[str | None] = mapped_column(String(128))
    deduplication_key: Mapped[str | None] = mapped_column(String(255), unique=True)

    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 100", name="valid_task_priority"),
        CheckConstraint(
            "estimated_minutes IS NULL OR estimated_minutes >= 0",
            name="nonnegative_estimated_minutes",
        ),
        Index("ix_tasks_work_queue", "status", "priority", "due_at"),
    )


class CompanyTask(Base):
    __tablename__ = "company_tasks"

    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class PersonTask(Base):
    __tablename__ = "person_tasks"

    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class JobTask(Base):
    __tablename__ = "job_tasks"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class ApplicationTask(Base):
    __tablename__ = "application_tasks"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class Interaction(Base, IdMixin):
    __tablename__ = "interactions"

    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    email_id: Mapped[str | None] = mapped_column(
        ForeignKey("emails.id", ondelete="SET NULL"), unique=True
    )
    meeting_id: Mapped[str | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), unique=True
    )
    kind: Mapped[InteractionKind] = mapped_column(
        enum_column(InteractionKind, "interaction_kind"), index=True
    )
    direction: Mapped[Direction] = mapped_column(enum_column(Direction, "interaction_direction"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)


class InteractionPerson(Base):
    __tablename__ = "interaction_people"

    interaction_id: Mapped[str] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str | None] = mapped_column(String(64))


class AgentRun(Base, IdMixin):
    __tablename__ = "agent_runs"

    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    agent_version: Mapped[str] = mapped_column(String(64))
    prompt_name: Mapped[str | None] = mapped_column(String(128))
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[AgentRunStatus] = mapped_column(
        enum_column(AgentRunStatus, "agent_run_status"), index=True
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ConnectorRun(Base, IdMixin):
    __tablename__ = "connector_runs"

    connector_name: Mapped[str] = mapped_column(String(128), index=True)
    operation: Mapped[str] = mapped_column(String(128))
    status: Mapped[ConnectorRunStatus] = mapped_column(
        enum_column(ConnectorRunStatus, "connector_run_status"), index=True
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    response_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ActionRequest(Base, IdMixin):
    __tablename__ = "action_requests"

    kind: Mapped[ActionKind] = mapped_column(enum_column(ActionKind, "action_kind"))
    status: Mapped[ActionStatus] = mapped_column(
        enum_column(ActionStatus, "action_status"),
        default=ActionStatus.PENDING,
        index=True,
    )
    summary: Mapped[str] = mapped_column(String(500))
    connector_name: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_action_requests_queue", "status", "requested_at"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(255), index=True)
    action_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_requests.id", ondelete="SET NULL"), index=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (Index("ix_audit_events_aggregate", "aggregate_type", "aggregate_id"),)


class DailyBrief(Base, IdMixin):
    __tablename__ = "daily_briefs"

    brief_date: Mapped[date] = mapped_column(Date, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    candidate_profile_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    estimated_minutes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("brief_date", "revision", name="uq_daily_briefs_revision"),
        CheckConstraint("revision > 0", name="positive_brief_revision"),
        CheckConstraint("estimated_minutes >= 0", name="nonnegative_brief_minutes"),
    )


class DailyBriefItem(Base, IdMixin):
    __tablename__ = "daily_brief_items"

    daily_brief_id: Mapped[str] = mapped_column(
        ForeignKey("daily_briefs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    recommendation: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    expected_roi: Mapped[float] = mapped_column(Float)
    estimated_minutes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("daily_brief_id", "rank", name="uq_daily_brief_items_rank"),
        CheckConstraint("rank > 0", name="positive_brief_item_rank"),
        CheckConstraint(
            "expected_roi >= 0 AND expected_roi <= 1",
            name="valid_expected_roi",
        ),
        CheckConstraint(
            "estimated_minutes >= 0",
            name="nonnegative_brief_item_minutes",
        ),
    )


class AssistantConversation(Base, IdMixin, TimestampMixin):
    """Legacy conversation storage, retained for existing database compatibility."""

    __tablename__ = "assistant_conversations"

    title: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[AssistantStatus] = mapped_column(
        enum_column(AssistantStatus, "assistant_status"),
        default=AssistantStatus.IDLE,
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class AssistantMessage(Base, IdMixin):
    """Legacy transcript storage; the command-line application does not replay it."""

    __tablename__ = "assistant_messages"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(128))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("conversation_id", "position", name="uq_assistant_message_position"),
    )


class AssistantToolCall(Base, IdMixin):
    """Legacy tool-call record; retained without any tool execution engine."""

    __tablename__ = "assistant_tool_calls"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="CASCADE"), index=True
    )
    tool_use_id: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    risk: Mapped[ToolRisk] = mapped_column(enum_column(ToolRisk, "tool_risk"))
    status: Mapped[ToolCallStatus] = mapped_column(
        enum_column(ToolCallStatus, "tool_call_status"), index=True
    )
    cost_note: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
