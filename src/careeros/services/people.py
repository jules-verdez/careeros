"""Decision-maker records with public-contact provenance."""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.enums import SourceKind
from careeros.database.models import (
    Company,
    Person,
    PersonContact,
    PersonEmployment,
    Source,
)
from careeros.services.audit import AuditService
from careeros.services.normalization import normalize_email, normalize_text


class PeopleService:
    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    def add_public_person(
        self,
        session: Session,
        *,
        company_id: str,
        full_name: str,
        title: str,
        influence_probability: float,
        confidence: float,
        linkedin_url: str | None,
        email: str | None,
        email_source_url: str | None,
        actor: str,
    ) -> Person:
        self._score("influence_probability", influence_probability)
        self._score("confidence", confidence)
        if session.get(Company, company_id) is None:
            raise LookupError(f"company not found: {company_id}")
        if email and not email_source_url:
            msg = "public email requires --email-source-url"
            raise ValueError(msg)
        normalized_name = normalize_text(full_name)
        person = None
        if linkedin_url:
            person = session.scalar(
                select(Person).where(Person.canonical_profile_url == linkedin_url)
            )
        if person is None:
            person = session.scalar(
                select(Person)
                .join(PersonEmployment, PersonEmployment.person_id == Person.id)
                .where(
                    Person.normalized_name == normalized_name,
                    PersonEmployment.company_id == company_id,
                    PersonEmployment.is_current.is_(True),
                )
            )
        if person is None:
            person = Person(
                full_name=full_name.strip(),
                normalized_name=normalized_name,
                canonical_profile_url=linkedin_url,
            )
            session.add(person)
            session.flush()
        employment = session.scalar(
            select(PersonEmployment).where(
                PersonEmployment.person_id == person.id,
                PersonEmployment.company_id == company_id,
                PersonEmployment.is_current.is_(True),
            )
        )
        if employment is None:
            employment = PersonEmployment(
                person_id=person.id,
                company_id=company_id,
                title=title.strip(),
                normalized_title=normalize_text(title),
                is_current=True,
                hiring_influence_probability=influence_probability,
                confidence=confidence,
            )
            session.add(employment)
        else:
            employment.title = title.strip()
            employment.normalized_title = normalize_text(title)
            employment.hiring_influence_probability = influence_probability
            employment.confidence = confidence
        if email and email_source_url:
            self._add_public_email(
                session,
                person=person,
                email=email,
                source_url=email_source_url,
            )
        self._audit.append(
            session,
            actor=actor,
            event_type="person.public_record_upserted",
            aggregate_type="person",
            aggregate_id=person.id,
            payload={
                "company_id": company_id,
                "title": title,
                "influence_probability": influence_probability,
                "confidence": confidence,
                "has_public_email": email is not None,
            },
        )
        return person

    @staticmethod
    def _add_public_email(
        session: Session,
        *,
        person: Person,
        email: str,
        source_url: str,
    ) -> None:
        normalized = normalize_email(email)
        existing = session.scalar(
            select(PersonContact).where(
                PersonContact.person_id == person.id,
                PersonContact.contact_type == "email",
                PersonContact.normalized_value == normalized,
            )
        )
        if existing is not None:
            return
        host = urlsplit(source_url).hostname or "public-web"
        source = session.scalar(
            select(Source).where(
                Source.kind == SourceKind.COMPANY_WEBSITE,
                Source.name == host,
            )
        )
        if source is None:
            source = Source(
                kind=SourceKind.COMPANY_WEBSITE,
                name=host,
                base_url=f"https://{host}" if host != "public-web" else source_url,
                connector_name="manual-public-record",
                metadata_json={},
            )
            session.add(source)
            session.flush()
        session.add(
            PersonContact(
                person_id=person.id,
                contact_type="email",
                value=email.strip(),
                normalized_value=normalized,
                is_public=True,
                source_id=source.id,
                public_source_url=source_url,
            )
        )

    @staticmethod
    def _score(name: str, value: float) -> None:
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be in [0, 1]")
