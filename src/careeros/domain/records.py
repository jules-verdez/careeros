"""Validated records crossing connector boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from careeros.database.enums import (
    ContactDeliveryStatus,
    DeliveryEventKind,
    EmploymentType,
    SourceKind,
)


class JobDiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: tuple[str, ...] = ()
    preferred_companies: tuple[str, ...] = ()
    technologies: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    #: Drop postings older than this instant. Outreach that references a role is only
    #: credible while the role is live, so freshness is a property of the query rather
    #: than something each connector invents in its own provider-specific input file.
    posted_since: datetime | None = None
    limit: int = Field(default=100, ge=1, le=10_000)

    @field_validator("posted_since")
    @classmethod
    def posted_since_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "posted_since must include a timezone"
            raise ValueError(msg)
        return value

    def is_within_window(self, posted_at: datetime | None) -> bool:
        """Whether one posting falls inside the freshness window.

        A posting whose date the source omitted is kept. A missing timestamp is not
        evidence that a role is stale, and silently dropping those would hide live roles
        at exactly the firms whose boards publish the least metadata.
        """
        if self.posted_since is None or posted_at is None:
            return True
        return posted_at >= self.posted_since

    def matches_title(self, title: str) -> bool:
        """Whether a job title matches any requested role, case-insensitively."""
        if not self.roles:
            return True
        haystack = title.casefold()
        return any(role.casefold() in haystack for role in self.roles)

    def matches_location(self, location: str | None) -> bool:
        """Whether a posting sits in a requested region.

        A posting with no stated location is kept for the same reason a missing date is:
        absence of metadata is not evidence of a mismatch.
        """
        if not self.regions:
            return True
        if not location:
            return True
        haystack = location.casefold()
        return any(region.casefold() in haystack for region in self.regions)


class JobRecord(BaseModel):
    """A connector's immutable observation of one advertised role."""

    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    source_name: str = Field(min_length=1, max_length=255)
    external_id: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl
    company_name: str = Field(min_length=1, max_length=255)
    company_domain: str | None = Field(default=None, max_length=255)
    company_website_url: HttpUrl | None = None
    title: str = Field(min_length=1, max_length=500)
    location: str | None = Field(default=None, max_length=255)
    remote_policy: str | None = Field(default=None, max_length=64)
    employment_type: EmploymentType | None = None
    compensation_text: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    closes_at: datetime | None = None
    confidence: float = Field(default=0.8, ge=0, le=1)
    requirements: tuple[str, ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("posted_at", "closes_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "job timestamps must include a timezone"
            raise ValueError(msg)
        return value

    @field_validator("company_domain")
    @classmethod
    def normalize_domain_input(cls, value: str | None) -> str | None:
        if value is None:
            return None
        domain = value.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if domain.startswith(prefix):
                domain = domain.removeprefix(prefix)
        return domain.rstrip("/").split("/", maxsplit=1)[0]


class ResearchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(min_length=1)
    structured: dict[str, Any]
    confidence: float = Field(ge=0, le=1)
    source_urls: tuple[HttpUrl, ...] = ()


class PersonCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    linkedin_url: HttpUrl | None = None
    github_url: HttpUrl | None = None
    website_url: HttpUrl | None = None
    public_email: str | None = Field(default=None, max_length=320)
    public_email_source_url: HttpUrl | None = None
    recent_publications: tuple[str, ...] = ()
    mutual_connections: tuple[str, ...] = ()
    influence_probability: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("public_email_source_url")
    @classmethod
    def public_email_requires_citation(
        cls,
        value: HttpUrl | None,
        info: Any,
    ) -> HttpUrl | None:
        email = info.data.get("public_email")
        if email and value is None:
            msg = "a public email must include the public page where it was found"
            raise ValueError(msg)
        return value


#: P05 caps an introductory email at 150 words. Validate the limit before staging a draft.
MAX_OUTREACH_WORDS = 150

#: Substrings that mark a redirect or tracking link. A recipient sees these as tracking
#: and refuses to click; the plain destination URL is the only acceptable form.
TRACKING_LINK_MARKERS = (
    "google.com/url?",
    "safelinks.protection.outlook.com",
    "utm_source=",
    "utm_medium=",
    "utm_campaign=",
    "mailtrack",
    "hubspotlinks",
)


class DraftMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1)
    paragraph_rationale: tuple[str, ...]
    evidence_document_ids: tuple[str, ...]

    @model_validator(mode="after")
    def body_is_short_and_links_are_plain(self) -> DraftMessage:
        words = len(self.body.split())
        if words > MAX_OUTREACH_WORDS:
            msg = f"outreach body is {words} words; the limit is {MAX_OUTREACH_WORDS}"
            raise ValueError(msg)
        lowered = self.body.casefold()
        for marker in TRACKING_LINK_MARKERS:
            if marker in lowered:
                msg = (
                    f"outreach body contains a redirect or tracking link ({marker}); "
                    "use the plain URL"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def every_paragraph_has_a_rationale(self) -> DraftMessage:
        paragraphs = tuple(part.strip() for part in self.body.split("\n\n") if part.strip())
        if len(paragraphs) != len(self.paragraph_rationale):
            msg = (
                "paragraph_rationale must contain exactly one explanation for each "
                f"non-empty body paragraph ({len(paragraphs)} required)"
            )
            raise ValueError(msg)
        return self


class DeliverabilityVerdict(BaseModel):
    """What a free check concluded about one address, and by which method.

    ``status`` reuses :class:`ContactDeliveryStatus` so a verdict maps straight onto the
    stored contact without a second vocabulary. ``method`` and ``detail`` are kept beside
    it because the same status means different things depending on how it was reached: an
    ``UNKNOWN`` from a DNS-only run means "not asked", while an ``UNKNOWN`` from an SMTP
    probe means "asked and the server declined to say".
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    domain: str = Field(default="", max_length=255)
    status: ContactDeliveryStatus
    method: str = Field(min_length=1, max_length=32)
    detail: str = Field(min_length=1)
    has_mx: bool
    #: ``None`` when the question was never asked, which is not the same as "no".
    is_catch_all: bool | None = None
    checked_at: datetime

    @property
    def is_sendable(self) -> bool:
        """True only where a mailbox was positively confirmed to exist."""
        return self.status is ContactDeliveryStatus.DELIVERABLE

    @property
    def is_disproved(self) -> bool:
        """True where the address was positively shown not to accept mail."""
        return self.status is ContactDeliveryStatus.INVALID


class BounceReport(BaseModel):
    """Structured delivery-status notification extracted from an inbound message."""

    model_config = ConfigDict(extra="forbid")

    kind: DeliveryEventKind
    recipient_address: str | None = Field(default=None, max_length=320)
    smtp_status: str | None = Field(default=None, max_length=32)
    diagnostic: str | None = None
    original_message_id: str | None = Field(default=None, max_length=998)


class MailboxMessage(BaseModel):
    """The minimum mailbox content required for reply and bounce reconciliation."""

    model_config = ConfigDict(extra="forbid")

    external_message_id: str = Field(min_length=1, max_length=500)
    external_thread_id: str | None = Field(default=None, max_length=500)
    rfc822_message_id: str | None = Field(default=None, max_length=998)
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: tuple[str, ...] = ()
    sender_address: str = Field(min_length=1, max_length=320)
    recipient_addresses: tuple[str, ...] = ()
    subject: str = Field(default="(no subject)", max_length=998)
    body: str = ""
    received_at: datetime
    automated: bool = False
    bounce: BounceReport | None = None

    @field_validator("received_at")
    @classmethod
    def received_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        return value


class ContactEnrichmentResult(BaseModel):
    """One provider observation; it is not automatically a public contact record."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    full_name: str | None = Field(default=None, max_length=255)
    email_address: str | None = Field(default=None, max_length=320)
    email_status: str = Field(min_length=1, max_length=64)
    is_verified: bool = False
    current_job_title: str | None = Field(default=None, max_length=255)
    linkedin_url: HttpUrl | None = None
    source_url: HttpUrl
    credits_spent: int = Field(default=0, ge=0, le=1)
    free_lookup: bool = False
    response_sha256: str = Field(min_length=64, max_length=64)


class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    description: str | None = None
    page_age: str | None = Field(default=None, max_length=128)
