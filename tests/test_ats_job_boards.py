from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from careeros.connectors.ats import AtsJobBoardConnector, JobBoard
from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.database.enums import SourceKind
from careeros.domain.records import JobDiscoveryQuery

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(hours=3)
LAST_YEAR = NOW - timedelta(days=400)


def _connector(
    board: JobBoard,
    payload: Any,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> AtsJobBoardConnector:
    connector = AtsJobBoardConnector(board, "acme", **kwargs)
    monkeypatch.setattr(connector, "_fetch", lambda: payload)
    return connector


def _greenhouse(job_id: int, title: str, published: str, location: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": location},
        "first_published": published,
        "content": "<p>role</p>",
    }


def test_freshness_window_drops_older_postings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outreach referencing a role is only credible while the role is live."""
    connector = _connector(
        JobBoard.GREENHOUSE,
        {
            "jobs": [
                _greenhouse(1, "Quant Researcher", YESTERDAY.isoformat(), "London, UK"),
                _greenhouse(2, "Quant Researcher", LAST_YEAR.isoformat(), "London, UK"),
            ]
        },
        monkeypatch,
    )
    fresh = list(connector.discover(JobDiscoveryQuery(posted_since=NOW - timedelta(hours=24))))
    assert [record.external_id for record in fresh] == ["1"]

    unbounded = list(connector.discover(JobDiscoveryQuery()))
    assert len(unbounded) == 2


def test_posting_without_a_date_survives_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing timestamp is not evidence a role is stale, so it must not be dropped."""
    connector = _connector(
        JobBoard.GREENHOUSE,
        {"jobs": [_greenhouse(3, "Quant Developer", "", "New York, NY")]},
        monkeypatch,
    )
    records = list(connector.discover(JobDiscoveryQuery(posted_since=NOW - timedelta(hours=1))))
    assert len(records) == 1
    assert records[0].posted_at is None


def test_role_and_region_filters_narrow_to_one_desk(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(
        JobBoard.GREENHOUSE,
        {
            "jobs": [
                _greenhouse(1, "Quant Researcher", YESTERDAY.isoformat(), "London, UK"),
                _greenhouse(2, "Recruiter", YESTERDAY.isoformat(), "London, UK"),
                _greenhouse(3, "Quant Researcher", YESTERDAY.isoformat(), "Singapore"),
            ]
        },
        monkeypatch,
    )
    query = JobDiscoveryQuery(roles=("quant",), regions=("London",))
    assert [record.external_id for record in connector.discover(query)] == ["1"]


def test_limit_stops_yielding(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(
        JobBoard.GREENHOUSE,
        {
            "jobs": [
                _greenhouse(n, "Engineer", YESTERDAY.isoformat(), "London") for n in range(1, 8)
            ]
        },
        monkeypatch,
    )
    assert len(list(connector.discover(JobDiscoveryQuery(limit=3)))) == 3


def test_lever_epoch_milliseconds_become_aware_datetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lever dates its postings in epoch milliseconds where the others send ISO 8601."""
    connector = _connector(
        JobBoard.LEVER,
        [
            {
                "id": "abc",
                "text": "Systematic Trader",
                "hostedUrl": "https://jobs.lever.co/acme/abc",
                "categories": {"location": "London"},
                "createdAt": int(YESTERDAY.timestamp() * 1000),
                "descriptionPlain": "trade",
            }
        ],
        monkeypatch,
        company_name="Acme Capital",
        company_domain="acme.com",
    )
    record = next(iter(connector.discover(JobDiscoveryQuery())))
    assert record.posted_at is not None
    assert record.posted_at.tzinfo is not None
    assert record.posted_at == YESTERDAY
    assert record.source_kind is SourceKind.LEVER
    assert record.company_name == "Acme Capital"
    # The domain is what later joins a posting to a Hunter lookup for the same firm.
    assert record.company_domain == "acme.com"


def test_ashby_zulu_timestamps_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(
        JobBoard.ASHBY,
        {
            "jobs": [
                {
                    "id": "xyz",
                    "title": "Research Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/xyz",
                    "location": "Remote",
                    "publishedAt": "2026-08-17T09:00:00.000Z",
                }
            ]
        },
        monkeypatch,
    )
    record = next(iter(connector.discover(JobDiscoveryQuery())))
    assert record.posted_at == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    assert record.source_kind is SourceKind.ASHBY


def test_smartrecruiters_pages_then_fetches_details_only_for_matching_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = AtsJobBoardConnector(
        JobBoard.SMARTRECRUITERS,
        "acme",
        company_name="Acme Capital",
        company_domain="acme.test",
    )
    listing = {
        "totalFound": 2,
        "content": [
            {
                "id": "quant-1",
                "name": "Quantitative Researcher",
                "releasedDate": YESTERDAY.isoformat(),
                "location": {"city": "London", "country": "GB", "remote": False},
                "typeOfEmployment": {"label": "Permanent"},
                "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/quant-1",
            },
            {
                "id": "sales-1",
                "name": "Sales Director",
                "releasedDate": YESTERDAY.isoformat(),
                "location": {"city": "London", "country": "GB", "remote": False},
            },
        ],
    }
    detail_ids: list[str] = []

    monkeypatch.setattr(
        connector,
        "_fetch_smartrecruiters_page",
        lambda *, offset, limit: listing,
    )

    def detail(posting_id: str) -> dict[str, Any]:
        detail_ids.append(posting_id)
        return {
            "id": posting_id,
            "applyUrl": "https://jobs.smartrecruiters.com/acme/quant-1",
            "jobAd": {
                "sections": {
                    "jobDescription": {
                        "title": "Job Description",
                        "text": "Build systematic research tools.",
                    },
                    "qualifications": {
                        "title": "Qualifications",
                        "text": "Python and statistics.",
                    },
                }
            },
        }

    monkeypatch.setattr(connector, "_fetch_smartrecruiters_detail", detail)
    records = list(
        connector.discover(JobDiscoveryQuery(roles=("quant",), regions=("London",), limit=10))
    )

    assert detail_ids == ["quant-1"]
    assert len(records) == 1
    record = records[0]
    assert record.source_kind is SourceKind.SMARTRECRUITERS
    assert record.source_url.encoded_string() == "https://jobs.smartrecruiters.com/acme/quant-1"
    assert record.company_name == "Acme Capital"
    assert record.company_domain == "acme.test"
    assert record.employment_type is not None
    assert record.employment_type.value == "full_time"
    assert "Python and statistics" in (record.description or "")


def test_posting_missing_a_url_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """JobRecord requires a source URL, so an unusable row is dropped, not raised on."""
    connector = _connector(
        JobBoard.GREENHOUSE,
        {
            "jobs": [
                {"id": 1, "title": "No URL", "first_published": YESTERDAY.isoformat()},
                _greenhouse(2, "Has URL", YESTERDAY.isoformat(), "London"),
            ]
        },
        monkeypatch,
    )
    assert [record.external_id for record in connector.discover(JobDiscoveryQuery())] == ["2"]


def test_unknown_slug_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong slug returns nothing and looks like a firm with no roles, so it must raise."""
    connector = AtsJobBoardConnector(JobBoard.GREENHOUSE, "nope")

    def explode() -> Any:
        raise ConnectorNotConfiguredError("No greenhouse board exists for slug 'nope'.")

    monkeypatch.setattr(connector, "_fetch", explode)
    with pytest.raises(ConnectorNotConfiguredError):
        connector.healthcheck()


def test_query_rejects_naive_posted_since() -> None:
    """Naive timestamps silently compare wrong against the aware dates boards return."""
    with pytest.raises(ValueError, match="timezone"):
        JobDiscoveryQuery(posted_since=datetime(2026, 8, 17, 12, 0))  # noqa: DTZ001
