"""Addresses a company publishes on its own site, with that page as the citation.

When a firm publishes ``recipient@employer.example`` on its professional contact page, that
page provides a direct citation for the address. A search-results URL alone cannot
establish that the address was explicitly published for professional contact.

Three constraints shape the implementation.

It reads ``robots.txt`` first and honours it, because the connector contract requires
respecting robots policy and because a firm that asks not to be crawled has said
something worth hearing before writing to them.

It fetches a short fixed list of conventional paths rather than spidering. A crawler
that follows links is a different kind of thing to point at somebody's website, and the
addresses worth having sit on ``/team``, ``/about`` or ``/contact`` in practice.

It reports what it found rather than inferring what it did not. Where a name sits beside
an address the pair is returned; where only an address appears it is returned alone, and
a role mailbox like ``info@`` is labelled as such instead of being dressed up as a person.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from careeros.connectors.base import ConnectorNotConfiguredError, PeopleDiscoveryConnector
from careeros.domain.records import PersonCandidate

USER_AGENT = "careeros-team-pages"

#: Contact details may be published on a careers or jobs subdomain rather than the apex.
CANDIDATE_HOST_PREFIXES = ("", "www.", "careers.", "jobs.", "about.")

#: Conventional locations for a page that lists people and how to reach them.
CANDIDATE_PATHS = (
    "/team",
    "/about",
    "/about-us",
    "/people",
    "/contact",
    "/contact-us",
    "/leadership",
    "/company/team",
    "/our-team",
    "/impressum",
    "/connect",
    "/",
)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

#: Local parts that address a function rather than a person.
ROLE_LOCAL_PARTS = frozenset(
    {
        "info",
        "contact",
        "hello",
        "hi",
        "team",
        "sales",
        "support",
        "help",
        "admin",
        "office",
        "press",
        "media",
        "jobs",
        "careers",
        "recruitment",
        "recruiting",
        "hr",
        "legal",
        "privacy",
        "gdpr",
        "dpo",
        "security",
        "abuse",
        "postmaster",
        "webmaster",
        "noreply",
        "no-reply",
        "donotreply",
        "mail",
        "enquiries",
        "inquiries",
        "general",
    }
)

#: Strings that appear inside asset filenames and tracking identifiers rather than mail.
NON_ADDRESS_MARKERS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".css",
    ".js",
    "@2x",
    "@3x",
    "sentry.io",
    "example.com",
    "example.org",
    "domain.com",
    "yourcompany",
)

#: Published for contact, so the record is strong; still an estimate, never a guarantee.
PUBLISHED_CONFIDENCE = 0.9
NEUTRAL_INFLUENCE = 0.5

_NAME_PATTERN = re.compile(r"^[A-Z][\w'\-]+(?: [A-Z][\w'\-.]+){1,3}$")


def looks_like_person_name(value: str) -> bool:
    """Whether a fragment of link or heading text reads as somebody's name."""
    cleaned = " ".join(value.split())
    return bool(_NAME_PATTERN.match(cleaned)) and "@" not in cleaned


@dataclass(frozen=True)
class PublishedContact:
    """One address a company prints on one of its own pages."""

    email: str
    page_url: str
    name: str | None = None
    context: str | None = None

    @property
    def local_part(self) -> str:
        return self.email.split("@", maxsplit=1)[0].casefold()

    @property
    def is_role_address(self) -> bool:
        """True for a functional mailbox rather than an individual's."""
        return self.local_part in ROLE_LOCAL_PARTS


class _ContactPageParser(HTMLParser):
    """Collects ``mailto:`` targets, their link text, and the heading above them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.mailto: list[tuple[str, str, str | None]] = []
        self.text_parts: list[str] = []
        self._heading: str | None = None
        self._in_heading = False
        self._pending: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"):
            self._in_heading = True
            self._heading = ""
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.casefold().startswith("mailto:"):
            address = href[7:].split("?", maxsplit=1)[0].strip()
            if address:
                self._pending = address
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"):
            self._in_heading = False
        if tag == "a" and self._pending is not None:
            self.mailto.append((self._pending, " ".join(self._link_text).strip(), self._heading))
            self._pending = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        self.text_parts.append(stripped)
        if self._in_heading and self._heading is not None:
            self._heading = f"{self._heading} {stripped}".strip()
        if self._pending is not None:
            self._link_text.append(stripped)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


class CompanyPageContactConnector(PeopleDiscoveryConnector):
    """Reads addresses a company publishes on its own pages, honouring robots.txt."""

    def __init__(self, *, timeout: float = 15.0, paths: tuple[str, ...] = CANDIDATE_PATHS) -> None:
        self._timeout = timeout
        self._paths = paths

    @property
    def name(self) -> str:
        return "company-page-contacts"

    def healthcheck(self) -> None:
        """Nothing to configure. The connector needs no key and no account."""
        if not self._paths:
            msg = "No candidate paths configured, so no page would ever be fetched."
            raise ConnectorNotConfiguredError(msg)

    def find_contacts_at(
        self, page_url: str, *, domain: str | None = None
    ) -> tuple[PublishedContact, ...]:
        """Read one page the operator already knows about, honouring its robots policy.

        Worth having beside the sweep because a person who has found the right page by
        hand should not have to hope a fixed path list rediscovers it.
        """
        parts = urlsplit(page_url)
        base = f"{parts.scheme or 'https'}://{parts.hostname or ''}"
        robots = self._robots(base)
        if robots is not None and not robots.can_fetch(USER_AGENT, page_url):
            return ()
        body = self._fetch(page_url)
        if body is None:
            return ()
        found = self._extract(body, page_url=page_url)
        if domain:
            wanted = domain.strip().casefold().lstrip("@")
            found = [contact for contact in found if _matches_domain(contact.email, wanted)]
        return tuple(sorted(found, key=lambda c: (c.is_role_address, c.email)))

    def find_contacts(
        self,
        domain: str,
        *,
        same_domain_only: bool = True,
    ) -> tuple[PublishedContact, ...]:
        """Fetch the conventional contact pages for ``domain`` and read the addresses off them.

        ``same_domain_only`` drops addresses that belong to somebody else. A team page
        routinely carries a design agency's address or a press contact at an outside firm,
        and attributing those to this company would be simply wrong.
        """
        apex = self._host(domain).removeprefix("www.")
        wanted = apex.casefold()
        found: dict[str, PublishedContact] = {}
        for prefix in CANDIDATE_HOST_PREFIXES:
            base = f"https://{prefix}{apex}"
            robots = self._robots(base)
            reachable = False
            for path in self._paths:
                url = urljoin(base, path)
                if robots is not None and not robots.can_fetch(USER_AGENT, url):
                    continue
                body = self._fetch(url)
                if body is None:
                    continue
                reachable = True
                for contact in self._extract(body, page_url=url):
                    if same_domain_only and not _matches_domain(contact.email, wanted):
                        continue
                    # First hit wins: earlier hosts and paths are the more specific ones.
                    found.setdefault(contact.email.casefold(), contact)
            if not reachable and prefix == "":
                continue
        ordered = sorted(found.values(), key=lambda c: (c.is_role_address, c.email))
        return tuple(ordered)

    def discover_people(self, company_name: str) -> Iterable[PersonCandidate]:
        """Yield named individuals published on ``company_name``, read as a domain."""
        for contact in self.find_contacts(company_name):
            if contact.is_role_address or not contact.name:
                continue
            yield PersonCandidate(
                full_name=contact.name[:255],
                role=(contact.context or "Listed on the company's public contact page")[:255],
                public_email=contact.email,
                public_email_source_url=contact.page_url,  # type: ignore[arg-type]
                website_url=contact.page_url,  # type: ignore[arg-type]
                influence_probability=NEUTRAL_INFLUENCE,
                confidence=PUBLISHED_CONFIDENCE,
            )

    # ------------------------------------------------------------------- internals

    @staticmethod
    def _host(domain: str) -> str:
        value = domain.strip()
        if "//" in value:
            return urlsplit(value).hostname or value
        return value.strip("/")

    def _robots(self, base: str) -> RobotFileParser | None:
        """Read the site's crawl policy. An unreadable file is treated as permissive.

        A missing or broken ``robots.txt`` is not a refusal, and RFC 9309 says as much:
        only a served file that disallows the path is a refusal.
        """
        parser = RobotFileParser()
        body = self._fetch(urljoin(base, "/robots.txt"))
        if body is None:
            return None
        parser.parse(body.splitlines())
        return parser

    def _fetch(self, url: str) -> str | None:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                charset: str = response.headers.get_content_charset() or "utf-8"
                body: bytes = response.read(2_000_000)
                return body.decode(charset, "replace")
        except (HTTPError, URLError, TimeoutError, ValueError):
            return None

    @staticmethod
    def _extract(body: str, *, page_url: str) -> list[PublishedContact]:
        parser = _ContactPageParser()
        try:
            parser.feed(body)
        except Exception:
            # Malformed markup must not abort a sweep across many hosts.
            return []
        results: list[PublishedContact] = []
        seen: set[str] = set()
        for address, link_text, heading in parser.mailto:
            if not _is_plausible(address) or address.casefold() in seen:
                continue
            seen.add(address.casefold())
            name = None
            if looks_like_person_name(link_text):
                name = " ".join(link_text.split())
            elif heading and looks_like_person_name(heading):
                name = " ".join(heading.split())
            results.append(
                PublishedContact(
                    email=address,
                    page_url=page_url,
                    name=name,
                    context=(heading or None) if heading != name else None,
                )
            )
        # Addresses printed as text rather than linked are common on legal and impressum
        # pages, so the visible text is scanned too, without a name attached.
        for match in EMAIL_PATTERN.finditer(parser.text):
            address = match.group(0)
            if not _is_plausible(address) or address.casefold() in seen:
                continue
            seen.add(address.casefold())
            results.append(PublishedContact(email=address, page_url=page_url))
        return results


def _matches_domain(email: str, wanted: str) -> bool:
    """Whether an address belongs to the company rather than to a third party on its page."""
    host = email.rsplit("@", maxsplit=1)[-1].casefold()
    return host == wanted or host.endswith(f".{wanted}")


def _is_plausible(address: str) -> bool:
    lowered = address.casefold()
    if any(marker in lowered for marker in NON_ADDRESS_MARKERS):
        return False
    return 5 <= len(address) <= 320 and address.count("@") == 1
