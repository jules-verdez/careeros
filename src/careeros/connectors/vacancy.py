"""Bounded public-page verification. HTTP success alone never proves a role is open."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from careeros.clock import Clock
from careeros.domain.workflow import VacancyEvidence, VacancyStatus
from careeros.services.normalization import normalize_text

MAX_BYTES = 2_000_000
USER_AGENT = "CareerOS-vacancy-check/1.0"


def public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username:
        raise ValueError("Use an unauthenticated public HTTP(S) vacancy URL")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Vacancy checks cannot access private or loopback addresses")


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PostingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[Any] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            try:
                self.documents.append(json.loads("".join(self._script)))
            except ValueError:
                pass
            self._script = None


def postings(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [posting for item in value for posting in postings(item)]
    if not isinstance(value, dict):
        return []
    types = value.get("@type", [])
    if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
        return [value]
    return postings(value.get("@graph"))


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # An employer date without a time zone is not an exact deadline.
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


class VacancyPageConnector:
    name = "public-vacancy-page"

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def healthcheck(self) -> None:
        pass

    def check(self, url: str, *, title: str, company: str) -> VacancyEvidence:
        now = self._clock.now()
        try:
            public_url(url)
            opener = build_opener(PublicRedirects())
            parts = urlsplit(url)
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            try:
                with opener.open(
                    Request(robots_url, headers={"User-Agent": USER_AGENT}), timeout=15
                ) as robots_response:
                    robots_body = robots_response.read(MAX_BYTES + 1)
                    if len(robots_body) > MAX_BYTES:
                        raise ValueError("Robots policy exceeds the response limit")
                    robots = RobotFileParser()
                    robots.parse(robots_body.decode("utf-8", errors="replace").splitlines())
                    if not robots.can_fetch(USER_AGENT, url):
                        raise ValueError(
                            "The employer's robots policy disallows this vacancy check"
                        )
            except HTTPError as error:
                if error.code != 404:
                    raise
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(request, timeout=15) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("Vacancy response exceeds the 2 MB limit")
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/ld+json", "application/json"}:
                    raise ValueError(f"Unsupported vacancy response: {content_type}")
                final_url = response.geturl()
                html = body.decode("utf-8", errors="replace")
        except (HTTPError, URLError, OSError, ValueError) as error:
            return VacancyEvidence(
                source_url=url,  # type: ignore[arg-type]
                status=VacancyStatus.UNREACHABLE,
                checked_at=now,
                evidence=(f"Could not verify the vacancy: {error}. This does not prove closure.",),
            )
        return self.parse(html, url=url, final_url=final_url, title=title, company=company)

    def parse(
        self, html: str, *, url: str, final_url: str, title: str, company: str
    ) -> VacancyEvidence:
        parser = PostingParser()
        parser.feed(html)
        try:
            parser.documents.append(json.loads(html))
        except ValueError:
            pass
        matches = []
        requested, final = urlsplit(url), urlsplit(final_url)
        same_vacancy = (requested.hostname or "").removeprefix("www.") == (
            final.hostname or ""
        ).removeprefix("www.") and requested.path.rstrip("/") == final.path.rstrip("/")
        for item in postings(parser.documents):
            organization = item.get("hiringOrganization")
            organization = organization if isinstance(organization, dict) else {}
            if (
                same_vacancy
                and normalize_text(str(item.get("title", ""))) == normalize_text(title)
                and normalize_text(str(organization.get("name", ""))) == normalize_text(company)
                and str(item.get("url", final_url)).rstrip("/") == final_url.rstrip("/")
            ):
                matches.append(item)
        status = VacancyStatus.UNKNOWN
        evidence = [f"Fetched {final_url}; no unique matching JobPosting was established."]
        deadline = published = None
        location = None
        sponsorship: tuple[str, ...] = ()
        if len(matches) == 1:
            posting = matches[0]
            deadline = timestamp(posting.get("validThrough"))
            published = timestamp(posting.get("datePosted"))
            # Date-only/unzoned deadlines stay unknown instead of inventing an offset.
            ambiguous_deadline = bool(posting.get("validThrough")) and deadline is None
            status = VacancyStatus.UNKNOWN if ambiguous_deadline else VacancyStatus.OPEN
            if deadline is not None and deadline <= self._clock.now():
                status = VacancyStatus.CLOSED
            evidence = [
                f"Matching employer JobPosting at {final_url}; status inferred from its metadata.",
                f"Published value: {posting.get('datePosted', 'not provided')}.",
                f"Deadline value: {posting.get('validThrough', 'not provided')}.",
            ]
            raw_location = posting.get("jobLocation")
            if raw_location:
                location = json.dumps(raw_location, ensure_ascii=False)
            description = re.sub(r"<[^>]+>", " ", str(posting.get("description", "")))
            sponsorship = tuple(
                sentence.strip()
                for sentence in re.split(r"[\n.!?]+", description)
                if re.search(r"\b(sponsor\w*|visa|work authori[sz]ation)\b", sentence, re.I)
                and sentence.strip()
            )
        return VacancyEvidence(
            source_url=url,  # type: ignore[arg-type]
            status=status,
            checked_at=self._clock.now(),
            evidence=tuple(evidence),
            published_at=published,
            deadline_at=deadline,
            location=location,
            sponsorship_evidence=sponsorship,
        )
