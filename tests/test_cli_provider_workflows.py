"""CLI integration checks against migrated temporary databases and test-only providers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from test_email_draft_execution import FakeMailConnector, _stage_email
from typer.testing import CliRunner

from careeros.cli import CliState, app
from careeros.clock import FixedClock
from careeros.config import Settings
from careeros.connectors.brave import BraveSearchConnector
from careeros.connectors.github_commits import CommitAuthor
from careeros.connectors.prospeo import ProspeoConnector
from careeros.connectors.sponsors import SponsorRegisterEntry, SponsorRegisterSnapshot
from careeros.connectors.team_pages import PublishedContact
from careeros.container import Container
from careeros.database.enums import (
    ActionKind,
    ActionStatus,
    ConnectorRunStatus,
    ContactDeliveryStatus,
    EmailStatus,
    SponsorMatchStatus,
)
from careeros.database.models import (
    ActionRequest,
    Company,
    CompanySponsorshipAssessment,
    ConnectorRun,
    ContactEnrichmentObservation,
    Document,
    Email,
    PersonContact,
)
from careeros.domain.records import DeliverabilityVerdict
from careeros.services.integration_health import ProbeResult, Readiness


@dataclass
class Operator:
    container: Container
    profile_id: str
    providers: dict[str, Any] = field(default_factory=dict)

    def invoke(self, *arguments: str, code: int = 0) -> str:
        result = CliRunner().invoke(
            app,
            [
                "--database-url",
                self.container.settings.database_url,
                *arguments,
            ],
        )
        assert result.exit_code == code, (result.output, result.exception)
        return result.output

    def person(self, *, email: str | None = None, source: str | None = None) -> str:
        with self.container.database.session() as session:
            company = Company(
                canonical_name="Example", normalized_name="example", domain="example.test"
            )
            session.add(company)
            session.flush()
            person = self.container.people.add_public_person(
                session,
                company_id=company.id,
                full_name="Sample Contact",
                title="Research lead",
                influence_probability=0.8,
                confidence=0.9,
                linkedin_url="https://www.linkedin.com/in/contact-example",
                email=email,
                email_source_url=source,
                actor="test",
            )
            return person.id


@pytest.fixture
def operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Operator]:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'operator.db'}")
    container = Container.build(settings, clock=FixedClock(datetime(2026, 7, 30, 9, tzinfo=UTC)))
    monkeypatch.setattr(CliState, "container", property(lambda _: container))
    result = CliRunner().invoke(
        app,
        [
            "--database-url",
            settings.database_url,
            "init",
            "--email",
            "candidate@example.test",
        ],
    )
    assert result.exit_code == 0, result.output
    with container.database.session() as session:
        profile = container.profiles.ensure_profile(session, name="Demo Candidate", actor="test")
        operator = Operator(container, profile.id)

    def require(capability: str) -> Any:
        # No test can accidentally resolve a real connector from the user's configuration.
        assert capability in operator.providers, f"Unexpected external capability: {capability}"
        provider = operator.providers[capability]
        provider.healthcheck()
        return provider

    monkeypatch.setattr(container.connectors, "require", require)
    yield operator
    container.database.dispose()


def test_cli_draft_and_send_require_separate_approvals_and_replay_once(operator: Operator) -> None:
    mail = FakeMailConnector()
    operator.providers["mail"] = mail
    email_id, action_id = _stage_email(operator.container, operator.profile_id)
    assert "CREATE A GMAIL DRAFT ONLY" in operator.invoke("action", "show", action_id)
    operator.invoke("action", "execute", action_id, code=2)
    assert mail.created == []
    operator.invoke("action", "approve", action_id)
    assert mail.created == []
    operator.invoke("action", "execute", action_id)
    assert "already exists" in operator.invoke("action", "execute", action_id)
    assert len(mail.created) == 1
    assert mail.sent == []

    operator.invoke("action", "request-send", email_id)
    with operator.container.database.session() as session:
        action = session.scalar(
            select(ActionRequest).where(ActionRequest.kind == ActionKind.SEND_EMAIL)
        )
        send_id = action.id
        assert action.status == ActionStatus.PENDING
    assert "SEND THIS EMAIL" in operator.invoke("action", "show", send_id)
    operator.invoke("action", "execute", send_id, code=2)
    assert mail.sent == []
    operator.invoke("action", "approve", send_id)
    operator.invoke("action", "execute", send_id)
    assert "already sent" in operator.invoke("action", "execute", send_id)
    assert len(mail.sent) == 1
    with operator.container.database.session() as session:
        email = session.get(Email, email_id)
        assert email.status == EmailStatus.SENT
        assert email.external_message_id == "provider-message-1"
        assert session.get(ActionRequest, send_id).status == ActionStatus.EXECUTED
        assert operator.container.audit.verify(session).valid


def test_unknown_and_unsupported_actions_fail_without_provider_calls(operator: Operator) -> None:
    assert "not found" in operator.invoke("action", "execute", "missing", code=2)
    with operator.container.database.session() as session:
        action = operator.container.approvals.request(
            session,
            kind=ActionKind.SUBMIT_APPLICATION,
            summary="Synthetic application",
            connector_name="unconfigured",
            payload={"job_id": "example"},
            idempotency_key="test-unsupported",
            requested_by="test",
        )
        action_id = action.id
    assert "example" in operator.invoke("action", "show", action_id)
    assert "no executable handler" in operator.invoke("action", "execute", action_id, code=2)
    with operator.container.database.session() as session:
        assert session.get(ActionRequest, action_id).status == ActionStatus.PENDING


def test_sponsor_alias_sync_and_filtered_listing_preserve_match_provenance(
    operator: Operator,
) -> None:
    operator.person()
    with operator.container.database.session() as session:
        company_id = session.scalar(select(Company.id))
    snapshot = SponsorRegisterSnapshot(
        entries=(
            SponsorRegisterEntry(
                organisation_name="Example Capital Limited",
                normalized_name="example capital limited",
                town_city="London",
                county=None,
                licence_type_rating="Worker (A rating)",
                route="Skilled Worker",
            ),
        ),
        source_url="https://example.test/register.csv",
        source_sha256="a" * 64,
        captured_at=operator.container.clock.now(),
    )
    operator.providers["sponsors.ukvi"] = SimpleNamespace(
        name="test-ukvi",
        healthcheck=lambda: None,
        fetch=lambda **kwargs: snapshot,
    )
    operator.invoke("sponsor", "sync")
    operator.invoke("sponsor", "alias", company_id, "Example Capital Limited")
    operator.invoke("sponsor", "sync", "--company", "Example")
    output = operator.invoke("sponsor", "list", "--status", "matched")
    assert "Example" in output
    assert "matched" in output
    with operator.container.database.session() as session:
        assessment = session.get(CompanySponsorshipAssessment, company_id)
        assert assessment.status == SponsorMatchStatus.MATCHED
        assert assessment.source_sha256 == snapshot.source_sha256
        assert all(
            run.status == ConnectorRunStatus.SUCCEEDED
            for run in session.scalars(select(ConnectorRun))
        )


def test_failed_sponsor_fetch_is_recorded_without_replacing_assessments(operator: Operator) -> None:
    def failed(**kwargs: object) -> None:
        raise RuntimeError("provider unavailable")

    operator.providers["sponsors.ukvi"] = SimpleNamespace(
        name="test-ukvi",
        healthcheck=lambda: None,
        fetch=failed,
    )
    operator.invoke("sponsor", "sync", code=1)
    with operator.container.database.session() as session:
        run = session.scalar(select(ConnectorRun))
        assert run.status == ConnectorRunStatus.FAILED
        assert "provider unavailable" in run.error
        assert session.scalar(select(func.count(CompanySponsorshipAssessment.company_id))) == 0


def test_paid_search_requires_explicit_spend_and_never_persists_snippets(
    operator: Operator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-only")
    calls: list[str] = []

    def requester(request: Any, timeout: float) -> dict[str, Any]:
        calls.append(request.full_url)
        return {
            "web": {
                "results": [
                    {
                        "title": "Employer team",
                        "url": "https://example.test/team",
                        "description": "PRIVATE TEST SNIPPET",
                        "page_age": "one day",
                    },
                ]
            }
        }

    operator.providers["search.brave"] = BraveSearchConnector(requester=requester)
    operator.invoke("evidence", "search", "example", code=2)
    operator.invoke(
        "evidence", "search", "example", "--spend-one-request", "--country", "GBR", code=2
    )
    assert calls == []
    assert "example.test" in operator.invoke("evidence", "search", "example", "--spend-one-request")
    assert len(calls) == 1
    with operator.container.database.session() as session:
        run = session.scalar(select(ConnectorRun))
        assert run.response_summary_json == {"results": 1, "requests_spent": 1}
        assert session.scalar(select(func.count(Document.id))) == 0
        assert "PRIVATE TEST SNIPPET" not in json.dumps(run.response_summary_json)


def test_paid_enrichment_is_an_observation_not_a_sendable_contact(
    operator: Operator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROSPEO_API_KEY", "test-only")
    person_id = operator.person()
    requests: list[Any] = []

    def requester(request: Any, timeout: float) -> dict[str, Any]:
        requests.append(request)
        return {
            "person": {
                "full_name": "Sample Contact",
                "email": {
                    "status": "VERIFIED",
                    "revealed": True,
                    "email": "recipient@example.test",
                },
            }
        }

    operator.providers["contact_enrichment.prospeo"] = ProspeoConnector(requester=requester)
    args = ("person", "enrich", person_id, "--reason", "hunter_missing")
    operator.invoke(*args, code=2)
    operator.invoke(*args, "--provider", "invalid", "--spend-one-credit", code=2)
    assert requests == []
    operator.invoke(*args, "--spend-one-credit")
    assert len(requests) == 1
    listing = operator.invoke("person", "enrichments", "--person-id", person_id)
    assert "Sample" in listing
    assert "Contact" in listing
    with operator.container.database.session() as session:
        assert session.scalar(select(func.count(ContactEnrichmentObservation.id))) == 1
        assert session.scalar(select(func.count(PersonContact.id))) == 0
        assert session.scalar(select(func.count(ActionRequest.id))) == 0


@pytest.mark.parametrize("provider", ["search.brave", "contact_enrichment.prospeo"])
def test_failed_paid_requests_finish_the_connector_ledger(
    operator: Operator, provider: str
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("test provider failed")

    operator.providers[provider] = SimpleNamespace(
        name="test-provider",
        healthcheck=lambda: None,
        search=failed,
        enrich_person=failed,
    )
    if provider == "search.brave":
        operator.invoke("evidence", "search", "example", "--spend-one-request", code=1)
    else:
        person_id = operator.person()
        operator.invoke(
            "person",
            "enrich",
            person_id,
            "--reason",
            "hunter_missing",
            "--spend-one-credit",
            code=1,
        )
    with operator.container.database.session() as session:
        run = session.scalar(select(ConnectorRun))
        assert run.status == ConnectorRunStatus.FAILED
        assert run.finished_at is not None
        assert session.scalar(select(func.count(ContactEnrichmentObservation.id))) == 0


def test_public_contact_tools_keep_preview_and_apply_distinct(operator: Operator) -> None:
    person_id = operator.person(
        email="recipient@example.test", source="https://google.com/search?q=contact"
    )
    contact = PublishedContact(
        email="recipient@example.test", name="Sample Contact", page_url="https://example.test/team"
    )
    calls: list[tuple[str, bool]] = []

    def check(email: str, *, allow_smtp_probe: bool = False) -> DeliverabilityVerdict:
        calls.append((email, allow_smtp_probe))
        return DeliverabilityVerdict(
            email=email,
            status=ContactDeliveryStatus.INVALID,
            method="dns_mx",
            detail="Domain has no mail exchanger",
            has_mx=False,
            checked_at=operator.container.clock.now(),
        )

    operator.providers["mail.deliverability"] = SimpleNamespace(
        healthcheck=lambda: None, check=check
    )
    operator.providers["people.company_pages"] = SimpleNamespace(
        healthcheck=lambda: None,
        find_contacts=lambda domain: (contact,),
        find_contacts_at=lambda url, domain: (contact,),
    )
    operator.providers["people.github_commits"] = SimpleNamespace(
        healthcheck=lambda: None,
        remaining_requests=lambda: 42,
        find_addresses=lambda *args, **kwargs: (
            CommitAuthor(
                email="recipient@example.test",
                name="Sample Contact",
                commits=2,
                citation_url="https://github.com/example/project/commit/123",
            ),
        ),
    )
    operator.invoke("contact", "check", "recipient@example.test")
    assert calls == [("recipient@example.test", False)]
    operator.invoke("contact", "recheck", "--only-unknown", "--limit", "1")
    with operator.container.database.session() as session:
        row = session.scalar(select(PersonContact).where(PersonContact.person_id == person_id))
        assert row.delivery_status == ContactDeliveryStatus.INVALID
        assert row.verified_at is None
        original_source = row.public_source_url
    operator.invoke("contact", "citations")
    with operator.container.database.session() as session:
        assert session.get(PersonContact, row.id).public_source_url == original_source
    operator.invoke("contact", "citations", "--apply")
    with operator.container.database.session() as session:
        assert session.get(PersonContact, row.id).public_source_url == contact.page_url
        assert operator.container.audit.verify(session).valid
    assert "Every stored address" in operator.invoke("contact", "citations")
    for args in [
        ("pages", "example.test"),
        ("pages", "example.test", "--url", contact.page_url),
        ("github", "example", "--domain", "example.test"),
    ]:
        assert "recipient@example.test" in operator.invoke("contact", *args)


@pytest.mark.parametrize("as_json", [False, True])
def test_doctor_reports_errors_to_automation_without_claiming_readiness(
    operator: Operator,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    seen: list[bool] = []

    def check_all(self: object, *, live: bool) -> list[ProbeResult]:
        seen.append(live)
        return [ProbeResult("gmail", Readiness.ERROR, "Test credentials unavailable")]

    monkeypatch.setattr("careeros.cli.IntegrationHealthService.check_all", check_all)
    output = operator.invoke("doctor", "--live", *(["--json"] if as_json else []), code=1)
    assert seen == [True]
    if as_json:
        assert json.loads(output)[0]["readiness"] == "error"
    else:
        assert "error" in output
