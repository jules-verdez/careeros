"""Controlled fallback enrichment that never bypasses public-email provenance gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import select

from careeros.clock import Clock
from careeros.connectors.base import ContactEnrichmentConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.database.enums import ContactEnrichmentReason
from careeros.database.models import (
    Company,
    ContactEnrichmentObservation,
    Person,
    PersonEmployment,
)
from careeros.database.session import Database
from careeros.domain.records import ContactEnrichmentResult
from careeros.services.audit import AuditService


@dataclass(frozen=True)
class ContactEnrichmentOutcome:
    observation_id: str
    result: ContactEnrichmentResult
    already_recorded: bool


class ContactEnrichmentService:
    def __init__(
        self,
        database: Database,
        connectors: ConnectorRegistry,
        clock: Clock,
        audit: AuditService,
    ) -> None:
        self._database = database
        self._connectors = connectors
        self._clock = clock
        self._audit = audit

    def enrich(
        self,
        person_id: str,
        *,
        capability: str,
        reason: ContactEnrichmentReason,
        allow_credit_spend: bool,
        actor: str,
    ) -> ContactEnrichmentOutcome:
        with self._database.session() as session:
            row = session.execute(
                select(Person, PersonEmployment, Company)
                .join(PersonEmployment, PersonEmployment.person_id == Person.id)
                .join(Company, Company.id == PersonEmployment.company_id)
                .where(
                    Person.id == person_id,
                    PersonEmployment.is_current.is_(True),
                )
                .order_by(PersonEmployment.confidence.desc())
                .limit(1)
            ).first()
            if row is None:
                raise LookupError(f"no current company is recorded for person: {person_id}")
            person, _, company = row
            if not company.domain:
                raise ValueError("the person's company needs a domain before enrichment")
            linkedin_url = (
                person.canonical_profile_url
                if person.canonical_profile_url
                and "linkedin.com/" in person.canonical_profile_url.casefold()
                else None
            )
            request = {
                "full_name": person.full_name,
                "company_name": company.canonical_name,
                "company_website": company.domain,
                "linkedin_url": linkedin_url,
                "person_id": person.id,
                "company_id": company.id,
            }

        connector = cast(
            ContactEnrichmentConnector,
            self._connectors.require(capability),
        )
        result = connector.enrich_person(
            full_name=str(request["full_name"]),
            company_name=str(request["company_name"]),
            company_website=str(request["company_website"]),
            linkedin_url=cast(str | None, request["linkedin_url"]),
            allow_credit_spend=allow_credit_spend,
        )

        with self._database.session() as session:
            existing = session.scalar(
                select(ContactEnrichmentObservation).where(
                    ContactEnrichmentObservation.person_id == person_id,
                    ContactEnrichmentObservation.provider == result.provider,
                    ContactEnrichmentObservation.response_sha256 == result.response_sha256,
                )
            )
            if existing is not None:
                return ContactEnrichmentOutcome(existing.id, result, True)
            observation = ContactEnrichmentObservation(
                person_id=person_id,
                company_id=str(request["company_id"]),
                provider=result.provider,
                fallback_reason=reason,
                email_address=result.email_address,
                provider_status=result.email_status,
                is_verified=result.is_verified,
                credits_spent=result.credits_spent,
                free_lookup=result.free_lookup,
                source_url=str(result.source_url),
                response_sha256=result.response_sha256,
                observed_at=self._clock.now(),
            )
            session.add(observation)
            session.flush()
            self._audit.append(
                session,
                actor=actor,
                event_type="contact.enrichment_observed",
                aggregate_type="person",
                aggregate_id=person_id,
                payload={
                    "provider": result.provider,
                    "fallback_reason": reason.value,
                    "provider_status": result.email_status,
                    "is_verified": result.is_verified,
                    "credits_spent": result.credits_spent,
                    "promoted_to_public_contact": False,
                },
            )
            return ContactEnrichmentOutcome(observation.id, result, False)
