"""Idempotent job observation ingestion."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import DocumentKind, RequirementKind
from careeros.database.models import (
    Company,
    CompanyAlias,
    Document,
    DocumentSource,
    Job,
    JobDocument,
    JobObservation,
    JobRequirement,
    Source,
)
from careeros.domain.records import JobRecord
from careeros.services.audit import AuditService
from careeros.services.normalization import (
    hash_payload,
    job_fingerprint,
    normalize_domain,
    normalize_text,
    sha256_text,
)


@dataclass
class IngestionSummary:
    records_seen: int = 0
    jobs_created: int = 0
    jobs_matched: int = 0
    observations_created: int = 0
    observations_updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "records_seen": self.records_seen,
            "jobs_created": self.jobs_created,
            "jobs_matched": self.jobs_matched,
            "observations_created": self.observations_created,
            "observations_updated": self.observations_updated,
        }


class JobIngestionService:
    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self._clock = clock
        self._audit = audit

    def ingest(
        self,
        session: Session,
        records: list[JobRecord],
        *,
        actor: str,
        connector_name: str,
    ) -> IngestionSummary:
        summary = IngestionSummary()
        for record in records:
            summary.records_seen += 1
            source = self._source(session, record, connector_name=connector_name)
            company = self._company(session, record)
            raw_document = self._document_for_record(session, record)
            self._link_document_source(session, raw_document.id, source.id)

            existing_observation = session.scalar(
                select(JobObservation).where(
                    JobObservation.source_id == source.id,
                    JobObservation.external_id == record.external_id,
                )
            )
            if existing_observation is not None:
                job = session.get(Job, existing_observation.job_id)
                if job is None:
                    msg = "job observation references a missing job"
                    raise RuntimeError(msg)
                self._refresh_job(job, record)
                existing_observation.observed_at = self._clock.now()
                existing_observation.source_url = str(record.source_url)
                existing_observation.source_confidence = record.confidence
                existing_observation.content_sha256 = raw_document.sha256 or ""
                existing_observation.raw_document_id = raw_document.id
                summary.jobs_matched += 1
                summary.observations_updated += 1
                self._audit_observation(
                    session,
                    actor=actor,
                    job=job,
                    observation=existing_observation,
                    event_type="job.observation_refreshed",
                )
                continue

            fingerprint = job_fingerprint(
                company_identity=record.company_domain or record.company_name,
                title=record.title,
                location=record.location,
                posted_at=record.posted_at,
            )
            job = session.scalar(select(Job).where(Job.canonical_fingerprint == fingerprint))
            if job is None:
                job = Job(
                    company_id=company.id,
                    canonical_fingerprint=fingerprint,
                    title=record.title.strip(),
                    normalized_title=normalize_text(record.title),
                    location=record.location,
                    remote_policy=record.remote_policy,
                    employment_type=record.employment_type,
                    compensation_text=record.compensation_text,
                    posted_at=record.posted_at,
                    closes_at=record.closes_at,
                    first_seen_at=self._clock.now(),
                    last_seen_at=self._clock.now(),
                )
                session.add(job)
                session.flush()
                summary.jobs_created += 1
            else:
                self._refresh_job(job, record)
                summary.jobs_matched += 1

            observation = JobObservation(
                job_id=job.id,
                source_id=source.id,
                external_id=record.external_id,
                source_url=str(record.source_url),
                observed_at=self._clock.now(),
                source_confidence=record.confidence,
                content_sha256=raw_document.sha256 or "",
                raw_document_id=raw_document.id,
            )
            session.add(observation)
            session.flush()
            summary.observations_created += 1
            self._add_description(session, job, record)
            self._add_requirements(session, job, record)
            self._audit_observation(
                session,
                actor=actor,
                job=job,
                observation=observation,
                event_type="job.observed",
            )
        return summary

    def _source(
        self,
        session: Session,
        record: JobRecord,
        *,
        connector_name: str,
    ) -> Source:
        source = session.scalar(
            select(Source).where(
                Source.kind == record.source_kind,
                Source.name == record.source_name,
            )
        )
        if source is None:
            source = Source(
                kind=record.source_kind,
                name=record.source_name,
                base_url=f"{record.source_url.scheme}://{record.source_url.host}",
                connector_name=connector_name,
                metadata_json={},
            )
            session.add(source)
            session.flush()
        return source

    @staticmethod
    def _company(session: Session, record: JobRecord) -> Company:
        normalized_name = normalize_text(record.company_name)
        domain = normalize_domain(record.company_domain) if record.company_domain else None
        company = None
        if domain is not None:
            company = session.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            company = session.scalar(
                select(Company).where(Company.normalized_name == normalized_name)
            )
        if company is None:
            company = Company(
                canonical_name=record.company_name.strip(),
                normalized_name=normalized_name,
                domain=domain,
                website_url=(
                    str(record.company_website_url) if record.company_website_url else None
                ),
            )
            session.add(company)
            session.flush()
            return company
        if company.normalized_name != normalized_name:
            alias = session.scalar(
                select(CompanyAlias).where(CompanyAlias.normalized_alias == normalized_name)
            )
            if alias is None:
                session.add(
                    CompanyAlias(
                        company_id=company.id,
                        alias=record.company_name.strip(),
                        normalized_alias=normalized_name,
                    )
                )
        if company.domain is None and domain is not None:
            company.domain = domain
        if company.website_url is None and record.company_website_url:
            company.website_url = str(record.company_website_url)
        return company

    @staticmethod
    def _document_for_record(session: Session, record: JobRecord) -> Document:
        raw_payload = record.model_dump(mode="json")
        digest = hash_payload(raw_payload)
        document = session.scalar(select(Document).where(Document.sha256 == digest))
        if document is None:
            document = Document(
                kind=DocumentKind.OTHER,
                title=f"{record.source_name}: {record.title}",
                media_type="application/json",
                content_json=raw_payload,
                sha256=digest,
                source_url=str(record.source_url),
            )
            session.add(document)
            session.flush()
        return document

    @staticmethod
    def _link_document_source(session: Session, document_id: str, source_id: str) -> None:
        link = session.get(DocumentSource, (document_id, source_id))
        if link is None:
            session.add(DocumentSource(document_id=document_id, source_id=source_id))

    def _refresh_job(self, job: Job, record: JobRecord) -> None:
        job.last_seen_at = self._clock.now()
        job.remote_policy = record.remote_policy or job.remote_policy
        job.compensation_text = record.compensation_text or job.compensation_text
        job.closes_at = record.closes_at or job.closes_at
        job.employment_type = record.employment_type or job.employment_type

    @staticmethod
    def _add_description(session: Session, job: Job, record: JobRecord) -> None:
        if not record.description:
            return
        digest = sha256_text(record.description)
        document = session.scalar(select(Document).where(Document.sha256 == digest))
        if document is None:
            document = Document(
                kind=DocumentKind.JOB_DESCRIPTION,
                title=f"{record.company_name} — {record.title}",
                media_type="text/plain",
                content_text=record.description,
                sha256=digest,
                source_url=str(record.source_url),
                captured_at=record.posted_at,
            )
            session.add(document)
            session.flush()
        link = session.get(JobDocument, (job.id, document.id, "description"))
        if link is None:
            session.add(
                JobDocument(
                    job_id=job.id,
                    document_id=document.id,
                    relationship="description",
                )
            )

    @staticmethod
    def _add_requirements(session: Session, job: Job, record: JobRecord) -> None:
        for requirement_text in record.requirements:
            normalized = normalize_text(requirement_text)
            existing = session.scalar(
                select(JobRequirement).where(
                    JobRequirement.job_id == job.id,
                    JobRequirement.kind == RequirementKind.OTHER,
                    JobRequirement.normalized_text == normalized,
                )
            )
            if existing is None:
                session.add(
                    JobRequirement(
                        job_id=job.id,
                        kind=RequirementKind.OTHER,
                        text=requirement_text.strip(),
                        normalized_text=normalized,
                        is_required=True,
                        weight=1.0,
                    )
                )

    def _audit_observation(
        self,
        session: Session,
        *,
        actor: str,
        job: Job,
        observation: JobObservation,
        event_type: str,
    ) -> None:
        self._audit.append(
            session,
            actor=actor,
            event_type=event_type,
            aggregate_type="job",
            aggregate_id=job.id,
            payload={
                "observation_id": observation.id,
                "source_id": observation.source_id,
                "external_id": observation.external_id,
                "content_sha256": observation.content_sha256,
            },
        )
