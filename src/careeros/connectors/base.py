"""Connector protocols.

Domain services know only these protocols. Concrete provider packages are loaded
at the application edge and cannot bypass approval services.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from careeros.domain.records import (
    ContactEnrichmentResult,
    DeliverabilityVerdict,
    JobDiscoveryQuery,
    JobRecord,
    MailboxMessage,
    PersonCandidate,
    ResearchArtifact,
    WebSearchResult,
)


class Connector(Protocol):
    @property
    def name(self) -> str:
        """Stable provider/adapter identifier used in logs."""

    def healthcheck(self) -> None:
        """Raise a descriptive exception when configuration is invalid."""


class JobDiscoveryConnector(Connector, Protocol):
    def discover(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        """Yield source observations without writing domain state."""


class ResearchConnector(Connector, Protocol):
    def research_company(self, company_name: str, *, as_of: datetime) -> ResearchArtifact:
        """Produce a cited research snapshot."""


class PeopleDiscoveryConnector(Connector, Protocol):
    def discover_people(self, company_name: str) -> Iterable[PersonCandidate]:
        """Yield public professional records with evidence."""


class ContactEnrichmentConnector(Connector, Protocol):
    def enrich_person(
        self,
        *,
        full_name: str,
        company_name: str,
        company_website: str,
        linkedin_url: str | None,
        allow_credit_spend: bool,
    ) -> ContactEnrichmentResult:
        """Resolve one known person, with an explicit maximum one-credit authorization."""


class WebSearchConnector(Connector, Protocol):
    def search(
        self,
        query: str,
        *,
        count: int,
        country: str,
        allow_request_spend: bool,
    ) -> tuple[WebSearchResult, ...]:
        """Run one explicitly authorized web-search request without persisting snippets."""


class MailDeliverabilityConnector(Connector, Protocol):
    """Judges whether an address can receive mail, without a vendor credit.

    Separated from :class:`ContactEnrichmentConnector` because it answers the opposite
    question. Enrichment asks a vendor to find an address and bills for the answer; this
    asks public infrastructure whether an address already in hand is worth sending to,
    and the cheap half of that costs nothing at all.
    """

    def mail_exchangers(self, domain: str) -> tuple[str, ...]:
        """Return the domain's mail exchangers; empty means it cannot receive mail."""

    def check(self, email: str, *, allow_smtp_probe: bool = False) -> DeliverabilityVerdict:
        """Judge one address. The SMTP half requires an explicit authorization."""


class MailConnector(Connector, Protocol):
    def list_messages(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """Read mail without mutating the mailbox."""

    def find_bounces(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """List delivery notifications without reading their content."""

    def find_application_notices(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """List applicant-tracking-system notices (acknowledgements, rejections) by id only."""

    def get_message(self, external_message_id: str) -> MailboxMessage:
        """Read one message selected by the reconciliation service."""

    def create_draft(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        in_reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Create a provider draft after an approved action."""

    def draft_identity(self, external_draft_id: str) -> tuple[str, str]:
        """Return ``(RFC Message-ID, thread ID)`` for an existing draft."""

    def send_draft(
        self,
        external_draft_id: str,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        rfc822_message_id: str,
        external_thread_id: str,
    ) -> str:
        """Send an unchanged existing draft after a separate approved action."""


class CalendarConnector(Connector, Protocol):
    def busy_intervals(
        self,
        *,
        starts_at: datetime,
        ends_at: datetime,
    ) -> Iterable[tuple[datetime, datetime]]:
        """Return occupied intervals."""

    def create_event(self, payload: dict[str, Any], *, idempotency_key: str) -> str:
        """Create an event after an approved action."""


class ConnectorNotConfiguredError(RuntimeError):
    """Raised instead of silently substituting fake integration data."""
