"""Exercise provider result boundaries without authentication, network, or paid runs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from careeros.connectors.apify import (
    ApifyGlassdoorConnector,
    ApifyGreenhouseConnector,
    ApifyLeverConnector,
    ApifyLinkedInProfileConnector,
)
from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.domain.records import JobDiscoveryQuery


class RunResult(BaseModel):
    status: str = "SUCCEEDED"
    dataset_id: str = Field(default="test-dataset", alias="defaultDatasetId")


class ProviderStub:
    def __init__(self, records: list[dict[str, Any]], run: Any = None) -> None:
        self.records = records
        self.run = run
        self.calls: list[dict[str, Any]] = []
        self.datasets: list[str] = []

    def actor(self, actor_id: str) -> ProviderStub:
        self.actor_id = actor_id
        return self

    def call(self, *, run_input: dict[str, Any]) -> Any:
        self.calls.append(run_input)
        return self.run

    def dataset(self, dataset_id: str) -> SimpleNamespace:
        self.datasets.append(dataset_id)
        return SimpleNamespace(iterate_items=lambda: iter(self.records))

    def user(self) -> SimpleNamespace:
        return SimpleNamespace(get=lambda: {"id": "test-user"})


@pytest.mark.parametrize(
    "connector_type,payload,title",
    [
        (
            ApifyGreenhouseConnector,
            {
                "id": 10,
                "title": "Researcher",
                "company": "Example",
                "location": {"name": "London"},
                "absolute_url": "https://example.test/jobs/10",
            },
            "Researcher",
        ),
        (
            ApifyLeverConnector,
            {
                "id": 10,
                "text": "Engineer",
                "company": "Example",
                "categories": {"location": "London"},
                "hostedUrl": "https://example.test/jobs/10",
            },
            "Engineer",
        ),
        (
            ApifyGlassdoorConnector,
            {
                "jobId": 10,
                "jobTitleText": "Analyst",
                "employerName": "Example",
                "locationName": "London",
                "url": "https://example.test/jobs/10",
            },
            "Analyst",
        ),
    ],
)
def test_successful_job_run_yields_valid_source_records(
    monkeypatch: pytest.MonkeyPatch, connector_type: type, payload: dict[str, Any], title: str
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "test-only")
    provider = ProviderStub([payload], RunResult())
    monkeypatch.setattr("careeros.connectors.apify.ApifyClient", lambda token: provider)
    connector = connector_type()
    connector.healthcheck()
    records = list(connector.discover(JobDiscoveryQuery(roles=("Research",), limit=250)))
    assert len(records) == 1
    assert records[0].title == title
    assert records[0].location == "London"
    assert records[0].company_name == "Example"
    assert records[0].external_id == "10"
    assert str(records[0].source_url) == "https://example.test/jobs/10"
    assert records[0].posted_at is None
    assert provider.datasets == ["test-dataset"]
    assert len(provider.calls) == 1
    assert connector.name.startswith("apify-")


@pytest.mark.parametrize(
    "connector_type",
    [
        ApifyGreenhouseConnector,
        ApifyLeverConnector,
        ApifyGlassdoorConnector,
        ApifyLinkedInProfileConnector,
    ],
)
def test_missing_authentication_fails_before_constructing_a_provider_client(
    monkeypatch: pytest.MonkeyPatch, connector_type: type
) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(
        "careeros.connectors.apify.ApifyClient", lambda token: pytest.fail("network")
    )
    connector = connector_type()
    with pytest.raises(ConnectorNotConfiguredError):
        connector.healthcheck()
    with pytest.raises(ConnectorNotConfiguredError):
        list(
            connector.discover_people("Example")
            if isinstance(connector, ApifyLinkedInProfileConnector)
            else connector.discover(JobDiscoveryQuery())
        )


@pytest.mark.parametrize(
    "run", [None, {"status": "FAILED"}, {"status": "RUNNING"}, {"status": "SUCCEEDED"}]
)
def test_incomplete_runs_never_read_or_present_partial_datasets(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "test-only")
    provider = ProviderStub([{"title": "Partial result"}], run)
    monkeypatch.setattr("careeros.connectors.apify.ApifyClient", lambda token: provider)
    with pytest.raises(RuntimeError):
        list(ApifyGreenhouseConnector().discover(JobDiscoveryQuery()))
    assert provider.datasets == []


def test_profile_discovery_does_not_promote_scraped_email_to_a_public_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "test-only")
    provider = ProviderStub(
        [
            {"fullName": ""},
            {
                "fullName": "Sample Contact",
                "headline": "Head of Research",
                "location": "London",
                "url": "https://www.linkedin.com/in/contact-example",
                "email": "unverified@example.test",
            },
        ],
        {"status": "SUCCEEDED", "defaultDatasetId": "test-dataset"},
    )
    monkeypatch.setattr("careeros.connectors.apify.ApifyClient", lambda token: provider)
    connector = ApifyLinkedInProfileConnector()
    connector.healthcheck()
    assert provider.calls == []
    people = list(connector.discover_people("Example"))
    assert len(people) == 1
    assert people[0].full_name == "Sample Contact"
    assert people[0].public_email is None
    assert people[0].public_email_source_url is None
    assert connector.name == "apify-linkedin-profiles"


def test_profile_authentication_failure_is_reported_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "test-only")

    def rejected(token: str) -> None:
        raise RuntimeError("authentication rejected")

    monkeypatch.setattr("careeros.connectors.apify.ApifyClient", rejected)
    with pytest.raises(ConnectorNotConfiguredError, match="authenticate"):
        ApifyLinkedInProfileConnector().healthcheck()
