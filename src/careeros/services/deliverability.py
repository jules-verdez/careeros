"""Re-judge stored contacts using the free checks, and repair weak citations.

Two jobs live here, and both exist to stop money being spent on questions that public
infrastructure answers for nothing.

The first is a sweep: every stored address is checked against DNS, and any that cannot
possibly receive mail is marked invalid before a verification credit or an outreach draft
is spent on it. At the free tier this is the difference between fifty verifications a
month and fifty *useful* ones.

The second is citation repair. A search-results URL does not establish that an address
was published for professional contact. Where a company publishes the same address on
its own site, that page is substituted and the substitution is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import select

from careeros.clock import Clock
from careeros.connectors.base import MailDeliverabilityConnector, PeopleDiscoveryConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.database.enums import ContactDeliveryStatus, SourceKind
from careeros.database.models import Company, Person, PersonContact, Source
from careeros.database.session import Database
from careeros.domain.records import DeliverabilityVerdict
from careeros.services.audit import AuditService

#: Citations that do not show the address on a page. A search-results URL records that
#: somebody searched, not that anybody published, so it cannot support a sendable email.
WEAK_CITATION_MARKERS = ("google.com/search", "bing.com/search", "duckduckgo.com/?q")


@dataclass(frozen=True)
class ContactCheck:
    contact_id: str
    person_name: str
    email: str
    previous_status: ContactDeliveryStatus
    verdict: DeliverabilityVerdict
    changed: bool


@dataclass(frozen=True)
class CitationRepair:
    contact_id: str
    email: str
    previous_url: str | None
    replacement_url: str
    matched: bool


class DeliverabilityService:
    """Applies free deliverability verdicts to stored contacts."""

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

    def recheck_contacts(
        self,
        *,
        actor: str,
        allow_smtp_probe: bool = False,
        limit: int | None = None,
        only_unknown: bool = False,
    ) -> tuple[ContactCheck, ...]:
        """Check every stored address and persist any status the free checks decide.

        A verdict only ever moves a contact when it is decisive. An ``UNKNOWN`` from a
        DNS-only run means the mailbox was never asked about, so overwriting a previously
        confirmed ``DELIVERABLE`` with it would destroy information rather than add any.
        """
        checker = cast(MailDeliverabilityConnector, self._connectors.require("mail.deliverability"))
        results: list[ContactCheck] = []
        with self._database.session() as session:
            query = (
                select(PersonContact, Person)
                .join(Person, Person.id == PersonContact.person_id)
                .where(PersonContact.contact_type == "email")
                .order_by(PersonContact.created_at)
            )
            if only_unknown:
                query = query.where(PersonContact.delivery_status == ContactDeliveryStatus.UNKNOWN)
            rows = session.execute(query).all()
            if limit is not None:
                rows = rows[:limit]
            for contact, person in rows:
                verdict = checker.check(contact.normalized_value, allow_smtp_probe=allow_smtp_probe)
                previous = contact.delivery_status
                changed = False
                if verdict.status is not ContactDeliveryStatus.UNKNOWN:
                    changed = previous is not verdict.status
                    contact.delivery_status = verdict.status
                    contact.delivery_checked_at = verdict.checked_at
                    contact.delivery_detail = f"{verdict.method}: {verdict.detail}"[:1000]
                    if verdict.is_disproved:
                        contact.verified_at = None
                    if changed:
                        self._audit.append(
                            session,
                            actor=actor,
                            event_type="contact.delivery_status_changed",
                            aggregate_type="person_contact",
                            aggregate_id=contact.id,
                            payload={
                                "delivery_status": verdict.status.value,
                                "previous_status": previous.value,
                                "method": verdict.method,
                                "detail": verdict.detail,
                                "cost": "none",
                            },
                        )
                results.append(
                    ContactCheck(
                        contact_id=contact.id,
                        person_name=person.full_name,
                        email=contact.normalized_value,
                        previous_status=previous,
                        verdict=verdict,
                        changed=changed,
                    )
                )
        return tuple(results)

    def repair_citations(self, *, actor: str, apply: bool = False) -> tuple[CitationRepair, ...]:
        """Replace search-result citations with the company page that prints the address.

        Nothing is written unless a page is found that actually contains the stored
        address. A repair that could not be evidenced leaves the weak citation in place,
        which is the honest outcome: the address stays unsendable until a source exists.
        """
        pages = cast(PeopleDiscoveryConnector, self._connectors.require("people.company_pages"))
        finder = getattr(pages, "find_contacts", None)
        if finder is None:  # pragma: no cover - protocol guarantees the concrete method
            msg = "the company-page connector cannot search a domain"
            raise RuntimeError(msg)
        repairs: list[CitationRepair] = []
        with self._database.session() as session:
            rows = session.execute(
                select(PersonContact, Person)
                .join(Person, Person.id == PersonContact.person_id)
                .where(PersonContact.contact_type == "email")
            ).all()
            for contact, _person in rows:
                url = contact.public_source_url or ""
                if not any(marker in url.casefold() for marker in WEAK_CITATION_MARKERS):
                    continue
                domain = contact.normalized_value.rsplit("@", maxsplit=1)[-1]
                published = {item.email.casefold(): item for item in finder(domain)}
                match = published.get(contact.normalized_value.casefold())
                if match is None:
                    repairs.append(
                        CitationRepair(
                            contact_id=contact.id,
                            email=contact.normalized_value,
                            previous_url=contact.public_source_url,
                            replacement_url="",
                            matched=False,
                        )
                    )
                    continue
                repairs.append(
                    CitationRepair(
                        contact_id=contact.id,
                        email=contact.normalized_value,
                        previous_url=contact.public_source_url,
                        replacement_url=match.page_url,
                        matched=True,
                    )
                )
                if not apply:
                    continue
                contact.public_source_url = match.page_url
                contact.source_id = self._page_source(session, match.page_url).id
                self._audit.append(
                    session,
                    actor=actor,
                    event_type="contact.citation_repaired",
                    aggregate_type="person_contact",
                    aggregate_id=contact.id,
                    payload={
                        "previous_source_url": url,
                        "public_source_url": match.page_url,
                        "reason": (
                            "A search-results URL does not carry the address. The "
                            "replacement is a company page on which it is published."
                        ),
                    },
                )
        return tuple(repairs)

    @staticmethod
    def _page_source(session: object, page_url: str) -> Source:
        from urllib.parse import urlsplit

        from sqlalchemy.orm import Session

        typed = cast(Session, session)
        host = urlsplit(page_url).hostname or "public-web"
        source = typed.scalar(
            select(Source).where(
                Source.kind == SourceKind.COMPANY_WEBSITE,
                Source.name == host,
            )
        )
        if source is None:
            source = Source(
                kind=SourceKind.COMPANY_WEBSITE,
                name=host,
                base_url=f"https://{host}",
                connector_name="company-page-contacts",
                metadata_json={},
            )
            typed.add(source)
            typed.flush()
        return source


def company_for_domain(session: object, domain: str) -> Company | None:
    """Find the stored company that owns a mail domain, if one is recorded."""
    from sqlalchemy.orm import Session

    typed = cast(Session, session)
    wanted = domain.strip().casefold()
    return typed.scalar(select(Company).where(Company.domain == wanted))


__all__ = [
    "CitationRepair",
    "ContactCheck",
    "DeliverabilityService",
    "company_for_domain",
]
