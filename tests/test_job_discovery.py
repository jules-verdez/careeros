from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from careeros.connectors.local_json import LocalJsonJobConnector
from careeros.container import Container
from careeros.database.models import AgentRun, Company, ConnectorRun, Job, JobObservation
from careeros.domain.records import JobDiscoveryQuery


def job_payload() -> list[dict[str, object]]:
    base = {
        "company_name": "Example Capital",
        "company_domain": "example.test",
        "title": "Quantitative Researcher",
        "location": "London, UK",
        "posted_at": "2026-07-28T09:00:00+00:00",
        "requirements": ["Python", "Statistics"],
    }
    return [
        {
            **base,
            "source_kind": "greenhouse",
            "source_name": "Example Greenhouse",
            "external_id": "job-1",
            "source_url": "https://boards.greenhouse.io/example/jobs/job-1",
            "description": "Systematic research.",
            "confidence": 0.95,
        },
        {
            **base,
            "source_kind": "career_page",
            "source_name": "Example Careers",
            "external_id": "quant-london",
            "source_url": "https://example.test/careers/quant-london",
            "description": "Systematic research.",
            "confidence": 0.99,
        },
        {
            "source_kind": "lever",
            "source_name": "Signal Lever",
            "external_id": "platform-1",
            "source_url": "https://jobs.lever.co/signal/platform-1",
            "company_name": "Signal Systems",
            "company_domain": "signal.test",
            "title": "Quant Platform Engineer",
            "location": "London, UK",
            "posted_at": "2026-07-30T08:00:00+00:00",
            "confidence": 0.9,
        },
    ]


def test_discovery_deduplicates_across_sources_and_replays(
    container: Container,
    active_profile: str,
    tmp_path: Path,
) -> None:
    del active_profile
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(job_payload()), encoding="utf-8")
    connector = LocalJsonJobConnector(path)

    first = container.discovery_agent.run(connector, JobDiscoveryQuery())
    assert first.records_seen == 3
    assert first.jobs_created == 2
    assert first.jobs_matched == 1
    assert first.observations_created == 3

    second = container.discovery_agent.run(connector, JobDiscoveryQuery())
    assert second.jobs_created == 0
    assert second.jobs_matched == 3
    assert second.observations_updated == 3

    with container.database.session() as session:
        assert session.scalar(select(func.count(Job.id))) == 2
        assert session.scalar(select(func.count(JobObservation.id))) == 3
        assert session.scalar(select(func.count(Company.id))) == 2
        assert session.scalar(select(func.count(AgentRun.id))) == 2
        assert session.scalar(select(func.count(ConnectorRun.id))) == 2
        assert container.audit.verify(session).valid


def test_local_json_connector_rejects_invalid_shape(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    path.write_text('{"not_jobs": []}', encoding="utf-8")
    connector = LocalJsonJobConnector(path)
    try:
        connector.discover(JobDiscoveryQuery())
    except ValueError as error:
        assert "'jobs' array" in str(error)
    else:
        raise AssertionError("invalid connector payload was accepted")
