"""Applicant-tracking-system job board connectors.

Greenhouse, Lever, Ashby and SmartRecruiters publish live postings as public JSON, with
timestamps and no credentials. For a workflow scoped to one company at a time that makes
a paid scraper the wrong default: these boards are free, are authoritative for the firm
that runs them, and carry the posting date that a 24-hour window depends on. A scraper
earns its keep only where a firm runs a bespoke careers page instead.

Built on ``urllib`` to match the Hunter connector: ``requests`` reaches this project only
transitively through ``apify-client`` and is not a declared dependency.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from careeros.connectors.base import ConnectorNotConfiguredError, JobDiscoveryConnector
from careeros.database.enums import EmploymentType, SourceKind
from careeros.domain.records import JobDiscoveryQuery, JobRecord

USER_AGENT = "careeros-job-discovery"


class JobBoard(StrEnum):
    """The boards that expose a company's postings without authentication."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"


#: Each board's public endpoint, keyed by the company's slug on that board.
BOARD_URLS: dict[JobBoard, str] = {
    JobBoard.GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    JobBoard.LEVER: "https://api.lever.co/v0/postings/{slug}?mode=json",
    JobBoard.ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    JobBoard.SMARTRECRUITERS: ("https://api.smartrecruiters.com/v1/companies/{slug}/postings"),
}

#: Which ``SourceKind`` each board's observations are recorded under. These all already
#: exist in the enum, which is stored as a checked string, so no migration is involved.
BOARD_SOURCE_KINDS: dict[JobBoard, SourceKind] = {
    JobBoard.GREENHOUSE: SourceKind.GREENHOUSE,
    JobBoard.LEVER: SourceKind.LEVER,
    JobBoard.ASHBY: SourceKind.ASHBY,
    JobBoard.SMARTRECRUITERS: SourceKind.SMARTRECRUITERS,
}


def _aware(value: Any) -> datetime | None:
    """Parse a board timestamp into an aware datetime, or None when unusable.

    Boards disagree about format: Greenhouse and Ashby send ISO 8601, Lever sends epoch
    milliseconds. A value that cannot be parsed becomes None rather than an exception,
    because one malformed date must not drop an otherwise live posting.
    """
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class AtsJobBoardConnector(JobDiscoveryConnector):
    """Reads one company's live postings from the board that company actually uses."""

    def __init__(
        self,
        board: JobBoard,
        slug: str,
        *,
        company_name: str | None = None,
        company_domain: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._board = board
        self._slug = slug
        # The board slug is usually the company name; keeping them separate lets the
        # stored record carry the firm's real name when the slug is an abbreviation.
        self._company_name = company_name or slug
        self._company_domain = company_domain
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"ats-{self._board.value}"

    def healthcheck(self) -> None:
        """Confirm the board slug resolves, so a typo fails here rather than silently.

        A wrong slug is the cheap version of an expensive mistake: it returns nothing and
        looks exactly like a company with no open roles. Checking it up front turns that
        into an error naming the slug.
        """
        try:
            if self._board is JobBoard.SMARTRECRUITERS:
                self._fetch_smartrecruiters_page(offset=0, limit=1)
            else:
                self._fetch()
        except ConnectorNotConfiguredError:
            raise
        except Exception as error:
            msg = f"Could not read the {self._board.value} board for {self._slug!r}: {error}"
            raise ConnectorNotConfiguredError(msg) from error

    def discover(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        """Yield postings matching the query's role, region and freshness constraints."""
        if self._board is JobBoard.SMARTRECRUITERS:
            yield from self._discover_smartrecruiters(query)
            return
        payload = self._fetch()
        yielded = 0
        for item in self._postings(payload):
            if yielded >= query.limit:
                return
            record = self._record(item)
            if record is None:
                continue
            if not query.is_within_window(record.posted_at):
                continue
            if not query.matches_title(record.title):
                continue
            if not query.matches_location(record.location):
                continue
            yielded += 1
            yield record

    def _postings(self, payload: Any) -> list[dict[str, Any]]:
        """Normalise the two envelope shapes: Lever returns a bare list, others wrap it."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            jobs = payload.get("jobs")
            if isinstance(jobs, list):
                return [item for item in jobs if isinstance(item, dict)]
        return []

    def _record(self, item: dict[str, Any]) -> JobRecord | None:
        builder = {
            JobBoard.GREENHOUSE: self._from_greenhouse,
            JobBoard.LEVER: self._from_lever,
            JobBoard.ASHBY: self._from_ashby,
            JobBoard.SMARTRECRUITERS: self._from_smartrecruiters,
        }[self._board]
        fields = builder(item)
        if not fields.get("external_id") or not fields.get("title"):
            return None
        if not fields.get("source_url"):
            return None
        return JobRecord(
            source_kind=BOARD_SOURCE_KINDS[self._board],
            source_name=self._board.value,
            company_name=self._company_name,
            company_domain=self._company_domain,
            raw=item,
            **fields,
        )

    @staticmethod
    def _from_greenhouse(item: dict[str, Any]) -> dict[str, Any]:
        location = item.get("location")
        return {
            "external_id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "source_url": item.get("absolute_url"),
            "location": (location or {}).get("name") if isinstance(location, dict) else location,
            # Greenhouse exposes first publication separately from the last edit; the
            # publication date is what "posted in the last 24 hours" means.
            "posted_at": _aware(item.get("first_published") or item.get("updated_at")),
            "description": item.get("content"),
        }

    @staticmethod
    def _from_lever(item: dict[str, Any]) -> dict[str, Any]:
        categories = item.get("categories")
        categories = categories if isinstance(categories, dict) else {}
        return {
            "external_id": str(item.get("id") or ""),
            "title": str(item.get("text") or ""),
            "source_url": item.get("hostedUrl"),
            "location": categories.get("location"),
            "posted_at": _aware(item.get("createdAt")),
            "description": item.get("descriptionPlain") or item.get("description"),
        }

    @staticmethod
    def _from_ashby(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "external_id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "source_url": item.get("jobUrl") or item.get("applyUrl"),
            "location": item.get("location"),
            "posted_at": _aware(item.get("publishedAt") or item.get("updatedAt")),
            "description": item.get("descriptionPlain"),
        }

    @staticmethod
    def _from_smartrecruiters(item: dict[str, Any]) -> dict[str, Any]:
        location = item.get("location")
        location = location if isinstance(location, dict) else {}
        type_of_employment = item.get("typeOfEmployment")
        type_of_employment = type_of_employment if isinstance(type_of_employment, dict) else {}
        return {
            "external_id": str(item.get("id") or item.get("uuid") or ""),
            "title": str(item.get("name") or ""),
            "source_url": item.get("applyUrl") or item.get("ref"),
            "location": _smartrecruiters_location(location),
            "remote_policy": (
                "remote" if location.get("remote") is True else "onsite_or_unspecified"
            ),
            "employment_type": _employment_type(type_of_employment.get("label")),
            "posted_at": _aware(item.get("releasedDate")),
            "description": _smartrecruiters_description(item),
        }

    def _discover_smartrecruiters(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        """Page the public Posting API and fetch details only for locally matched roles."""
        offset = 0
        yielded = 0
        page_size = 100
        while yielded < query.limit:
            payload = self._fetch_smartrecruiters_page(offset=offset, limit=page_size)
            content = payload.get("content") if isinstance(payload, dict) else None
            postings = (
                [item for item in content if isinstance(item, dict)]
                if isinstance(content, list)
                else []
            )
            if not postings:
                return
            for item in postings:
                title = str(item.get("name") or "")
                location = item.get("location")
                location = location if isinstance(location, dict) else {}
                posted_at = _aware(item.get("releasedDate"))
                if not query.is_within_window(posted_at):
                    continue
                if not query.matches_title(title):
                    continue
                if not query.matches_location(_smartrecruiters_location(location)):
                    continue
                posting_id = str(item.get("id") or item.get("uuid") or "")
                if not posting_id:
                    continue
                detail = self._fetch_smartrecruiters_detail(posting_id)
                merged = {**item, **detail}
                fields = self._from_smartrecruiters(merged)
                if not fields.get("source_url") or not fields.get("title"):
                    continue
                yield JobRecord(
                    source_kind=SourceKind.SMARTRECRUITERS,
                    source_name=JobBoard.SMARTRECRUITERS.value,
                    company_name=self._company_name,
                    company_domain=self._company_domain,
                    raw={"listing": item, "details": detail},
                    **fields,
                )
                yielded += 1
                if yielded >= query.limit:
                    return
            total = int(payload.get("totalFound") or 0)
            offset += len(postings)
            if offset >= total:
                return

    def _fetch_smartrecruiters_page(self, *, offset: int, limit: int) -> dict[str, Any]:
        base = BOARD_URLS[JobBoard.SMARTRECRUITERS].format(slug=quote(self._slug, safe=""))
        payload = self._fetch_url(f"{base}?{urlencode({'offset': offset, 'limit': limit})}")
        return payload if isinstance(payload, dict) else {}

    def _fetch_smartrecruiters_detail(self, posting_id: str) -> dict[str, Any]:
        base = BOARD_URLS[JobBoard.SMARTRECRUITERS].format(slug=quote(self._slug, safe=""))
        payload = self._fetch_url(f"{base}/{quote(posting_id, safe='')}")
        return payload if isinstance(payload, dict) else {}

    def _fetch(self) -> Any:
        url = BOARD_URLS[self._board].format(slug=quote(self._slug, safe=""))
        return self._fetch_url(url)

    def _fetch_url(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                msg = (
                    f"No {self._board.value} board exists for slug {self._slug!r}. "
                    "Check the slug on the company's careers page before running again."
                )
                raise ConnectorNotConfiguredError(msg) from error
            raise RuntimeError(
                f"{self._board.value} returned {error.code} for {self._slug!r}"
            ) from error
        except URLError as error:
            raise RuntimeError(f"{self._board.value} is unreachable: {error.reason}") from error


def _smartrecruiters_location(location: dict[str, Any]) -> str | None:
    parts = [location.get(key) for key in ("city", "region", "country")]
    rendered = ", ".join(str(part) for part in parts if part)
    if rendered:
        return rendered
    return "Remote" if location.get("remote") is True else None


def _smartrecruiters_description(item: dict[str, Any]) -> str | None:
    job_ad = item.get("jobAd")
    job_ad = job_ad if isinstance(job_ad, dict) else {}
    sections = job_ad.get("sections")
    sections = sections if isinstance(sections, dict) else {}
    rendered: list[str] = []
    for key in (
        "companyDescription",
        "jobDescription",
        "qualifications",
        "additionalInformation",
    ):
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        text = unescape(str(section.get("text") or "").strip())
        if text:
            rendered.append(f"{title}\n{text}" if title else text)
    return "\n\n".join(rendered) or None


def _employment_type(value: Any) -> EmploymentType | None:
    label = str(value or "").casefold()
    if "intern" in label:
        return EmploymentType.INTERNSHIP
    if "part" in label:
        return EmploymentType.PART_TIME
    if "contract" in label or "freelance" in label:
        return EmploymentType.CONTRACT
    if "temporary" in label or "fixed term" in label:
        return EmploymentType.TEMPORARY
    if any(token in label for token in ("full", "permanent", "regular")):
        return EmploymentType.FULL_TIME
    return None
