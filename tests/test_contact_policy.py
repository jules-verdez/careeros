from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXED_NOW, PROJECT_ROOT
from sqlalchemy import select

from careeros.clock import FixedClock
from careeros.config import Settings
from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.base import Base
from careeros.database.enums import ActionStatus
from careeros.database.models import ActionRequest, Company, Email
from careeros.domain.records import DraftMessage
from careeros.services.contact_policy import ContactExcludedError, ContactPolicy
from careeros.services.email_drafts import EmailDraftExecutor
from careeros.services.normalization import normalize_text
from careeros.services.outreach import OutreachService

MESSAGE = DraftMessage(
    subject="Research role",
    body="Hello,\n\nThis is a demonstration request.",
    paragraph_rationale=("Greeting.", "One request."),
    evidence_document_ids=(),
)


class RecordingMail:
    name = "google-workspace-gmail"

    def __init__(self) -> None:
        self.created: list[str] = []

    def healthcheck(self) -> None:
        return None

    def list_messages(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return []

    def create_draft(self, *, recipient: str, **_: Any) -> str:
        self.created.append(recipient)
        return "provider-draft-1"


Build = Callable[..., tuple[Container, str]]


@pytest.fixture
def build(tmp_path: Path) -> Iterator[Build]:
    """Containers over one shared database file, each with its own exclusion list."""
    built: list[Container] = []

    def factory(*entries: str) -> tuple[Container, str]:
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'career.db'}",
            candidate_name="Demo Candidate",
            do_not_contact=entries,
        )
        container = Container.build(
            settings, clock=FixedClock(FIXED_NOW), prompt_root=PROJECT_ROOT / "prompts"
        )
        built.append(container)
        Base.metadata.create_all(container.database.engine)
        with container.database.session() as session:
            profile = container.profiles.ensure_profile(
                session, name="Demo Candidate", email="candidate@example.com", actor="test"
            )
            return container, profile.id

    yield factory
    for container in built:
        container.database.dispose()


def add_person(container: Container, name: str, email: str) -> str:
    with container.database.session() as session:
        company = session.scalar(
            select(Company).where(Company.normalized_name == normalize_text("Example Capital"))
        )
        if company is None:
            company = Company(
                canonical_name="Example Capital",
                normalized_name=normalize_text("Example Capital"),
            )
            session.add(company)
            session.flush()
        person = container.people.add_public_person(
            session,
            company_id=company.id,
            full_name=name,
            title="Head of Research",
            influence_probability=0.9,
            confidence=0.9,
            linkedin_url=None,
            email=email,
            email_source_url="https://example.test/team",
            actor="test",
        )
        return person.id


def stage(container: Container, profile_id: str, person_id: str) -> tuple[str, str]:
    with container.database.session() as session:
        email, action_id = OutreachService(container.approvals, container.audit).stage_email(
            session,
            profile_id=profile_id,
            person_id=person_id,
            message=MESSAGE,
            connector_name="gmail",
            actor="test",
        )
        return email.id, action_id


def test_settings_read_one_delimited_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CAREEROS_DO_NOT_CONTACT",
        "Sample Contact; recipient@example.test,@blocked.test\nOther Contact",
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.do_not_contact == (
        "Sample Contact",
        "recipient@example.test",
        "@blocked.test",
        "Other Contact",
    )
    monkeypatch.delenv("CAREEROS_DO_NOT_CONTACT")
    assert Settings(_env_file=None).do_not_contact == ()  # type: ignore[call-arg]


def test_policy_matches_addresses_domains_and_normalized_names() -> None:
    policy = ContactPolicy(["Sample  CONTACT", "recipient.other@navy.test", "@blocked.test", " "])
    assert policy.entries == ("Sample  CONTACT", "recipient.other@navy.test", "@blocked.test")

    def rule(**kwargs: Any) -> str | None:
        match = policy.match(**kwargs)
        return match.rule if match else None

    assert rule(names=["sample contact"]) == "name"
    assert rule(addresses=["RECIPIENT.OTHER@navy.test"]) == "address"
    assert rule(addresses=["x@blocked.test"]) == "domain"
    assert rule(addresses=["x@eu.blocked.test"]) == "domain"
    assert rule(addresses=["x@notblocked.test"]) is None
    assert rule(names=["Another Contact"]) is None
    assert ContactPolicy().match(addresses=["anyone@example.test"]) is None


def test_excluded_person_cannot_be_drafted_for(build: Build) -> None:
    container, profile_id = build("Sample Contact")
    person_id = add_person(container, "Sample Contact", "recipient@example.test")
    service = OutreachService(container.approvals, container.audit, container.contact_policy)
    with container.database.session() as session:
        with pytest.raises(ContactExcludedError, match="name rule: Sample Contact"):
            service.stage_email(
                session,
                profile_id=profile_id,
                person_id=person_id,
                message=MESSAGE,
                connector_name="gmail",
                actor="test",
            )
    with container.database.session() as session:
        assert session.scalar(select(Email.id)) is None


def test_approval_refuses_an_excluded_recipient_but_not_others(build: Build) -> None:
    container, profile_id = build("@blocked.test")
    _, blocked = stage(
        container, profile_id, add_person(container, "Sample Contact", "recipient@blocked.test")
    )
    _, allowed = stage(
        container, profile_id, add_person(container, "Second Contact", "recipient.two@example.test")
    )
    with container.database.session() as session:
        with pytest.raises(ContactExcludedError, match="domain rule"):
            container.approvals.approve(session, blocked, reviewer="Demo Candidate")
    with container.database.session() as session:
        approved = container.approvals.approve(session, allowed, reviewer="Demo Candidate")
        assert approved.status is ActionStatus.APPROVED
        pending = session.get(ActionRequest, blocked)
        assert pending is not None and pending.status is ActionStatus.PENDING


def test_exclusion_added_after_approval_still_stops_execution(build: Build) -> None:
    lenient, profile_id = build()
    email_id, action_id = stage(
        lenient, profile_id, add_person(lenient, "Sample Contact", "recipient@example.test")
    )
    with lenient.database.session() as session:
        lenient.approvals.approve(session, action_id, reviewer="Demo Candidate")

    strict, _ = build("recipient@example.test")
    mail = RecordingMail()
    registry = ConnectorRegistry()
    registry.register("mail", mail)
    executor = EmailDraftExecutor(
        strict.database,
        registry,
        strict.clock,
        strict.audit,
        contact_policy=strict.contact_policy,
    )
    with pytest.raises(ContactExcludedError, match="address rule"):
        executor.execute(action_id, actor="Demo Candidate")
    assert mail.created == []

    # Queuing a send for the same message is refused before any provider is touched.
    with strict.database.session() as session:
        email = session.get(Email, email_id)
        assert email is not None
        email.external_message_id = "provider-draft-1"
    with strict.database.session() as session:
        with pytest.raises(ContactExcludedError):
            strict.email_sends.request(session, email_id, actor="Demo Candidate")


def test_action_match_reads_payload_recipient_and_ignores_unknown_rows(build: Build) -> None:
    container, _ = build("recipient@example.test")
    person_id = add_person(container, "Sample Contact", "recipient@example.test")
    with container.database.session() as session:
        policy = container.contact_policy
        assert policy.person_match(session, person_id) is not None
        assert policy.person_match(session, "missing") is None
        assert policy.email_match(session, "missing") is None
        direct = ActionRequest(payload_json={"recipient": "RECIPIENT@example.test"})
        assert policy.action_match(session, direct) is not None
        assert policy.action_match(session, ActionRequest(payload_json={})) is None
