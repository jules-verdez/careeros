from __future__ import annotations

from datetime import UTC, datetime, timedelta

from careeros.services.normalization import (
    hash_payload,
    job_fingerprint,
    normalize_domain,
    normalize_text,
    serialize_instant,
)


def test_normalization_is_stable() -> None:
    assert normalize_text("  Quant—Research  ") == "quant research"
    assert normalize_domain("https://WWW.Example.COM/jobs") == "example.com"
    assert hash_payload({"b": 2, "a": 1}) == hash_payload({"a": 1, "b": 2})


def test_job_fingerprint_deduplicates_same_week_but_not_reposts() -> None:
    first = datetime(2026, 7, 28, tzinfo=UTC)
    same_week = first + timedelta(days=1)
    next_week = first + timedelta(days=7)
    common = {
        "company_identity": "Example Capital",
        "title": "Quantitative Researcher",
        "location": "London, UK",
    }
    assert job_fingerprint(**common, posted_at=first) == job_fingerprint(
        **common,
        posted_at=same_week,
    )
    assert job_fingerprint(**common, posted_at=first) != job_fingerprint(
        **common,
        posted_at=next_week,
    )


def test_instant_serialization_treats_naive_sqlite_value_as_utc() -> None:
    aware = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
    naive = aware.replace(tzinfo=None)
    assert serialize_instant(aware) == serialize_instant(naive)
