"""Official UKVI Worker and Temporary Worker sponsor-register connector."""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from careeros.connectors.base import Connector
from careeros.services.normalization import normalize_text

REGISTER_PAGE_URL = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)
ALLOWED_ASSET_HOST = "assets.publishing.service.gov.uk"
USER_AGENT = "careeros-sponsor-register"
MAX_REGISTER_BYTES = 25 * 1024 * 1024

BytesReader = Callable[[str, float], bytes]


@dataclass(frozen=True)
class SponsorRegisterEntry:
    organisation_name: str
    normalized_name: str
    town_city: str | None
    county: str | None
    licence_type_rating: str | None
    route: str | None


@dataclass(frozen=True)
class SponsorRegisterSnapshot:
    entries: tuple[SponsorRegisterEntry, ...]
    source_url: str
    source_sha256: str
    captured_at: datetime


class _CsvLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href and urlsplit(href).path.casefold().endswith(".csv"):
            self.links.append(href)


class UkSponsorRegisterConnector(Connector):
    """Downloads and parses the current official register without third-party data."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        reader: BytesReader | None = None,
    ) -> None:
        self._timeout = timeout
        self._reader = reader or self._read_url

    @property
    def name(self) -> str:
        return "ukvi-sponsor-register"

    def healthcheck(self) -> None:
        # There is no key to validate. The actual fetch is the operation, and doing it
        # here would download a 10 MB register twice for every sync.
        return None

    def fetch(self, *, as_of: datetime) -> SponsorRegisterSnapshot:
        page = self._reader(REGISTER_PAGE_URL, self._timeout)
        parser = _CsvLinkParser()
        parser.feed(page.decode("utf-8", errors="replace"))
        if not parser.links:
            raise RuntimeError("The GOV.UK sponsor-register page contains no CSV document link.")
        source_url = urljoin(REGISTER_PAGE_URL, parser.links[0])
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != ALLOWED_ASSET_HOST:
            raise RuntimeError(f"Refusing an unexpected sponsor-register asset URL: {source_url}")

        payload = self._reader(source_url, self._timeout)
        if len(payload) > MAX_REGISTER_BYTES:
            raise RuntimeError("The sponsor register exceeds the 25 MiB safety limit.")
        entries = self._parse_csv(payload)
        if not entries:
            raise RuntimeError("The official sponsor-register CSV contained no usable rows.")
        return SponsorRegisterSnapshot(
            entries=entries,
            source_url=source_url,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            captured_at=as_of,
        )

    @staticmethod
    def _parse_csv(payload: bytes) -> tuple[SponsorRegisterEntry, ...]:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        parsed: list[SponsorRegisterEntry] = []
        for raw in reader:
            row = {
                normalize_text(key): (value or "").strip()
                for key, value in raw.items()
                if key is not None
            }
            organisation = row.get("organisation name") or row.get("organization name")
            if not organisation:
                continue
            parsed.append(
                SponsorRegisterEntry(
                    organisation_name=organisation,
                    normalized_name=normalize_sponsor_name(organisation),
                    town_city=row.get("town city") or None,
                    county=row.get("county") or None,
                    licence_type_rating=(
                        row.get("type rating") or row.get("type and rating") or None
                    ),
                    route=row.get("route") or None,
                )
            )
        return tuple(parsed)

    @staticmethod
    def _read_url(url: str, timeout: float) -> bytes:
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/csv,text/html"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_REGISTER_BYTES:
                    raise RuntimeError("The sponsor register exceeds the 25 MiB safety limit.")
                return bytes(response.read(MAX_REGISTER_BYTES + 1))
        except HTTPError as error:
            raise RuntimeError(f"GOV.UK returned HTTP {error.code} for {url}") from error
        except URLError as error:
            raise RuntimeError(f"GOV.UK sponsor register is unreachable: {error.reason}") from error


def normalize_sponsor_name(value: str) -> str:
    """Canonicalise punctuation while preserving legally meaningful words."""
    return normalize_text(value.replace("&", " and "))
