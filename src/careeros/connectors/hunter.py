"""Hunter.io connector.

An address inferred from a name pattern is not evidence of a reachable mailbox.
This connector uses addresses observed on public pages and retains those pages as
the citations :class:`PersonCandidate` requires for stored contacts.

Deliberately built on ``urllib`` rather than ``requests``: the latter is only present
transitively via ``apify-client`` and is not a declared dependency of this project.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import HttpUrl

from careeros.connectors.base import (
    ConnectorNotConfiguredError,
    PeopleDiscoveryConnector,
)
from careeros.domain.records import PersonCandidate

API_ROOT = "https://api.hunter.io/v2"

#: Hunter reports an address it cannot decide on as ``accept_all``: the domain answers
#: 250 to every recipient, so neither Hunter nor an SMTP probe can confirm the mailbox
#: exists. An inconclusive result must never be treated as confirmed delivery.
UNVERIFIABLE_STATUS = "accept_all"

#: Hunter has no notion of a contact's seniority, so candidates surfaced here take a
#: neutral prior rather than borrowing the optimistic constant the Apify path uses.
NEUTRAL_INFLUENCE = 0.5


def first_source_url(payload: dict[str, Any]) -> str | None:
    """Return the first public page Hunter cites for an address, if it cites one."""
    for source in payload.get("sources") or ():
        uri = source.get("uri")
        if uri:
            return str(uri)
    return None


@dataclass(frozen=True)
class EmailVerdict:
    """What Hunter knows about one address."""

    email: str | None
    status: str
    score: int
    source_url: str | None

    @property
    def is_sendable(self) -> bool:
        """True only for an address Hunter positively confirms."""
        return self.status == "valid"

    @property
    def is_unverifiable(self) -> bool:
        """True when the domain accepts all recipients, so no verdict is possible."""
        return self.status == UNVERIFIABLE_STATUS


@dataclass(frozen=True)
class DomainContact:
    """One address Hunter has already seen on a public page for a domain."""

    email: str
    first_name: str | None
    last_name: str | None
    position: str | None
    confidence: int
    source_url: str | None
    is_generic: bool

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> DomainContact:
        return cls(
            email=str(item["value"]),
            first_name=item.get("first_name") or None,
            last_name=item.get("last_name") or None,
            position=item.get("position") or None,
            confidence=int(item.get("confidence") or 0),
            source_url=first_source_url(item),
            is_generic=str(item.get("type") or "").casefold() == "generic",
        )


@dataclass(frozen=True)
class DomainProfile:
    """What Hunter already knows about a mail domain.

    This exists to be read *before* a scraper is paid to guess. A firm with a known
    address pattern needs names, not addresses, and names are the cheap half; a firm
    Hunter has never seen and that has no public GitHub org cannot be reached by
    spending more, only by changing source. Both facts are visible here for one
    search credit, which is the point.
    """

    domain: str
    organization: str | None
    pattern: str | None
    accept_all: bool
    webmail: bool
    total_known: int
    contacts: tuple[DomainContact, ...]

    @classmethod
    def from_envelope(cls, body: dict[str, Any], *, domain: str) -> DomainProfile:
        """Build a profile from a whole Hunter response body.

        Kept separate from the request so a response cached on disk parses by exactly
        the same rules as a live one. A cached answer that were parsed differently would
        quietly change a targeting decision between runs.
        """
        data = body.get("data") or {}
        meta = body.get("meta") or {}
        emails = data.get("emails") or []
        return cls(
            domain=str(data.get("domain") or domain),
            organization=data.get("organization") or None,
            pattern=data.get("pattern") or None,
            accept_all=bool(data.get("accept_all")),
            webmail=bool(data.get("webmail")),
            # Hunter reports the full count in the envelope and only ``limit`` rows in
            # the body, so a caller that counted ``contacts`` would understate a large
            # domain and misjudge it as unreachable.
            total_known=int(meta.get("results") or len(emails)),
            contacts=tuple(DomainContact.from_item(item) for item in emails if item.get("value")),
        )

    @property
    def named_contacts(self) -> tuple[DomainContact, ...]:
        """Contacts belonging to a person, excluding ``info@``-style role addresses."""
        return tuple(contact for contact in self.contacts if not contact.is_generic)

    @property
    def has_pattern(self) -> bool:
        """True when Hunter can construct an address from a name at this domain."""
        return bool(self.pattern)

    @property
    def is_catch_all(self) -> bool:
        """True when the domain answers 250 to every recipient, so no address verifies.

        Named separately from :attr:`accept_all` because the consequence, not the flag,
        is what callers act on: at a catch-all domain a ``valid`` verdict is unobtainable
        and a pattern-built address stays a guess no matter how plausible it looks.
        """
        return self.accept_all


class HunterConnector(PeopleDiscoveryConnector):
    """Finds and verifies public work addresses via Hunter.io."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._key = os.environ.get("HUNTER_API_KEY")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "hunter-io"

    def healthcheck(self) -> None:
        if not self._key:
            msg = "HUNTER_API_KEY environment variable is not set."
            raise ConnectorNotConfiguredError(msg)
        try:
            self._get("account")
        except ConnectorNotConfiguredError:
            raise
        except Exception as error:
            msg = f"Failed to authenticate with Hunter.io: {error}"
            raise ConnectorNotConfiguredError(msg) from error

    def account(self) -> dict[str, Any]:
        """Return plan and remaining-credit information. Never includes the key."""
        return self._get("account")

    def find_email(
        self,
        *,
        domain: str,
        first_name: str,
        last_name: str,
    ) -> EmailVerdict:
        """Resolve a name at a domain to the address Hunter has seen for that person."""
        payload = self._get(
            "email-finder",
            domain=domain,
            first_name=first_name,
            last_name=last_name,
        )
        return self._verdict(payload)

    def verify_email(self, email: str) -> EmailVerdict:
        """Check an address that already exists, without asking Hunter to guess one."""
        payload = self._get("email-verifier", email=email)
        return self._verdict(payload)

    def domain_search(self, domain: str, *, limit: int = 10) -> DomainProfile:
        """Report what Hunter knows about ``domain`` without asking it to guess anything.

        One search credit buys the address pattern, the catch-all flag and the count of
        addresses Hunter has already observed. Read together those decide whether a
        scraper run at this firm can pay for itself, so this is the call that belongs
        *before* the spend rather than after it.
        """
        body = self._get_envelope("domain-search", domain=domain, limit=limit)
        return DomainProfile.from_envelope(body, domain=domain)

    def domain_search_envelope(self, domain: str, *, limit: int = 10) -> dict[str, Any]:
        """Return the raw ``domain-search`` body, for callers that cache what they pay for."""
        return self._get_envelope("domain-search", domain=domain, limit=limit)

    def discover_people(self, company_name: str) -> Iterable[PersonCandidate]:
        """Yield public contacts for a domain, each carrying the page it was found on.

        ``company_name`` is the mail domain rather than a display name, because that is
        what Hunter keys on. A contact whose address has no public source is yielded
        without one: :class:`PersonCandidate` refuses to store an uncited email, and
        dropping the person entirely would hide a real lead.
        """
        if not self._key:
            raise ConnectorNotConfiguredError("HUNTER_API_KEY not set.")

        payload = self._get("domain-search", domain=company_name, limit=25)
        for item in payload.get("emails", []):
            full_name = " ".join(
                part for part in (item.get("first_name"), item.get("last_name")) if part
            ).strip()
            if not full_name:
                continue

            source_url = self._first_source(item)
            has_citation = bool(source_url)
            score = int(item.get("confidence") or 0)

            yield PersonCandidate(
                full_name=full_name[:255],
                role=(item.get("position") or "Unknown Role")[:255],
                location=None,
                linkedin_url=item.get("linkedin") or None,
                github_url=None,
                website_url=None,
                public_email=item.get("value") if has_citation else None,
                public_email_source_url=HttpUrl(source_url) if source_url else None,
                recent_publications=(),
                mutual_connections=(),
                influence_probability=NEUTRAL_INFLUENCE,
                confidence=score / 100,
            )

    def _verdict(self, payload: dict[str, Any]) -> EmailVerdict:
        score = int(payload.get("score") or 0)
        return EmailVerdict(
            email=payload.get("email"),
            status=str(payload.get("status") or "unknown"),
            score=score,
            source_url=self._first_source(payload),
        )

    @staticmethod
    def _first_source(payload: dict[str, Any]) -> str | None:
        return first_source_url(payload)

    def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        data = self._get_envelope(endpoint, **params).get("data")
        return data if isinstance(data, dict) else {}

    def _get_envelope(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """Return the whole response body, including the ``meta`` block ``_get`` drops."""
        if not self._key:
            raise ConnectorNotConfiguredError("HUNTER_API_KEY not set.")

        query = urlencode({**params, "api_key": self._key})
        request = Request(f"{API_ROOT}/{endpoint}?{query}", method="GET")
        try:
            with urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise self._http_error(error) from error
        except URLError as error:
            msg = f"Hunter.io is unreachable: {error.reason}"
            raise RuntimeError(msg) from error

        return body if isinstance(body, dict) else {}

    @staticmethod
    def _http_error(error: HTTPError) -> Exception:
        """Map Hunter's status codes onto the distinction the domain layer cares about.

        A missing or exhausted key is a configuration fault and must fail closed; a
        rate limit or outage is transient and belongs to the caller's retry policy.
        """
        detail = ""
        try:
            payload = json.loads(error.read().decode("utf-8"))
            errors = payload.get("errors") or []
            if errors:
                detail = str(errors[0].get("details") or "")
        except Exception:
            # The status code is the signal; an unparseable body just costs detail.
            detail = ""

        if error.code in (401, 403):
            return ConnectorNotConfiguredError(
                f"Hunter.io rejected HUNTER_API_KEY: {detail or error.reason}"
            )
        if error.code == 402:
            return ConnectorNotConfiguredError(
                f"Hunter.io credits exhausted: {detail or error.reason}"
            )
        if error.code == 429:
            # Hunter spends 429 on two different things: a genuine rate limit, which
            # passes on its own, and an account restriction, which never does. Calling
            # the second transient would send a caller into a retry loop that cannot
            # succeed, so it fails closed instead.
            if "restrict" in detail.casefold():
                return ConnectorNotConfiguredError(
                    f"Hunter.io account is restricted, resolve it at hunter.io: {detail}"
                )
            return RuntimeError(f"Hunter.io rate limit reached: {detail or error.reason}")
        return RuntimeError(f"Hunter.io request failed ({error.code}): {detail or error.reason}")
