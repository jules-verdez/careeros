"""Canonicalization and content hashing."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

_NON_ALPHANUMERIC = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = _NON_ALPHANUMERIC.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def normalize_domain(value: str) -> str:
    candidate = value.strip().casefold()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    hostname = urlsplit(candidate).hostname
    if hostname is None:
        msg = f"invalid domain: {value}"
        raise ValueError(msg)
    return hostname.removeprefix("www.")


def stable_json(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, datetime):
            return serialize_instant(item)
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"cannot serialize {type(item).__name__}")

    return json.dumps(
        value,
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_payload(value: Any) -> str:
    return sha256_text(stable_json(value))


def serialize_instant(value: datetime) -> str:
    if value.tzinfo is None:
        utc_value = value.replace(tzinfo=UTC)
    else:
        utc_value = value.astimezone(UTC)
    return utc_value.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def job_fingerprint(
    *,
    company_identity: str,
    title: str,
    location: str | None,
    posted_at: datetime | None,
) -> str:
    """Create a cross-source identity key.

    The posting week separates genuinely repeated vacancies while absorbing
    small source-to-source discrepancies in the advertised date.
    """

    if posted_at is None:
        posting_week = "undated"
    else:
        iso_year, iso_week, _ = posted_at.date().isocalendar()
        posting_week = f"{iso_year}-W{iso_week:02d}"
    identity = {
        "company": normalize_text(company_identity),
        "title": normalize_text(title),
        "location": normalize_text(location or "unspecified"),
        "posting_week": posting_week,
    }
    return hash_payload(identity)
