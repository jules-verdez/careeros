from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.enums import ActionKind, ActionStatus, DocumentKind, EmailStatus
from careeros.database.models import ActionRequest, Company, Document, Email
from careeros.domain.records import DraftMessage
from careeros.services.email_drafts import (
    EmailDraftExecutionError,
    EmailDraftExecutor,
    EmailDraftPayloadMismatchError,
)
from careeros.services.email_sends import (
    EmailSendExecutionError,
    EmailSendExecutor,
    EmailSendPayloadMismatchError,
)
from careeros.services.normalization import normalize_text, sha256_text
from careeros.services.outreach import OutreachService


class FakeMailConnector:
    name = "google-workspace-gmail"

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.sent: list[dict[str, str]] = []

    def healthcheck(self) -> None:
        return None

    def list_messages(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return []

    def create_draft(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        in_reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        self.created.append(
            {
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        return "provider-draft-1"

    def draft_identity(self, external_draft_id: str) -> tuple[str, str]:
        assert external_draft_id == "provider-draft-1"
        return "<provider-rfc822-message-id@example.test>", "provider-thread-1"

    def send_draft(
        self,
        external_draft_id: str,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        rfc822_message_id: str,
        external_thread_id: str,
    ) -> str:
        self.sent.append(
            {
                "external_draft_id": external_draft_id,
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "idempotency_key": idempotency_key,
                "rfc822_message_id": rfc822_message_id,
                "external_thread_id": external_thread_id,
            }
        )
        return "provider-message-1"


def _stage_email(container: Container, profile_id: str) -> tuple[str, str]:
    with container.database.session() as session:
        company = Company(
            canonical_name="Example Capital",
            normalized_name=normalize_text("Example Capital"),
            domain="example.test",
        )
        evidence = Document(
            kind=DocumentKind.JOB_DESCRIPTION,
            title="Python quant developer",
            media_type="text/plain",
            content_text="A live role.",
            sha256=sha256_text("A live role."),
            source_url="https://example.test/jobs/1",
        )
        session.add_all([company, evidence])
        session.flush()
        person = container.people.add_public_person(
            session,
            company_id=company.id,
            full_name="Sample Contact",
            title="Quant Technology Director",
            influence_probability=0.9,
            confidence=0.9,
            linkedin_url=None,
            email="recipient@example.test",
            email_source_url="https://example.test/team/contact",
            actor="test",
        )
        person_id = person.id
        evidence_id = evidence.id

    message = DraftMessage(
        subject="Python quant developer role",
        body="Hi Sample,\n\nThis is a demonstration request.",
        paragraph_rationale=("Greeting.", "Specific request."),
        evidence_document_ids=(evidence_id,),
    )
    with container.database.session() as session:
        email, action_id = OutreachService(container.approvals, container.audit).stage_email(
            session,
            profile_id=profile_id,
            person_id=person_id,
            message=message,
            connector_name="gmail",
            actor="test",
        )
        return email.id, action_id


def _executor(container: Container, fake: FakeMailConnector) -> EmailDraftExecutor:
    registry = ConnectorRegistry()
    registry.register("mail", fake)
    return EmailDraftExecutor(container.database, registry, container.clock, container.audit)


def _send_executor(container: Container, fake: FakeMailConnector) -> EmailSendExecutor:
    registry = ConnectorRegistry()
    registry.register("mail", fake)
    return EmailSendExecutor(
        container.database,
        registry,
        container.clock,
        container.audit,
        container.approvals,
    )


def _materialize_draft(
    container: Container,
    profile_id: str,
    fake: FakeMailConnector,
) -> tuple[str, str]:
    email_id, action_id = _stage_email(container, profile_id)
    with container.database.session() as session:
        container.approvals.approve(session, action_id, reviewer="Demo Candidate")
    _executor(container, fake).execute(action_id, actor="Demo Candidate")
    return email_id, action_id


def test_pending_action_cannot_create_an_external_draft(
    container: Container,
    active_profile: str,
) -> None:
    _, action_id = _stage_email(container, active_profile)
    fake = FakeMailConnector()

    with pytest.raises(EmailDraftExecutionError, match="pending"):
        _executor(container, fake).execute(action_id, actor="test")
    assert fake.created == []


def test_approved_action_creates_one_draft_and_persists_the_provider_id(
    container: Container,
    active_profile: str,
) -> None:
    email_id, action_id = _stage_email(container, active_profile)
    with container.database.session() as session:
        container.approvals.approve(session, action_id, reviewer="Demo Candidate")
    fake = FakeMailConnector()
    executor = _executor(container, fake)

    first = executor.execute(action_id, actor="Demo Candidate")
    second = executor.execute(action_id, actor="Demo Candidate")

    assert first.external_draft_id == "provider-draft-1"
    assert not first.already_executed
    assert second.already_executed
    assert len(fake.created) == 1
    assert fake.sent == []
    with container.database.session() as session:
        action = session.get(ActionRequest, action_id)
        email = session.get(Email, email_id)
        assert action is not None and action.status is ActionStatus.EXECUTED
        assert email is not None and email.external_message_id == "provider-draft-1"


def test_approved_packet_fails_closed_if_the_email_changes(
    container: Container,
    active_profile: str,
) -> None:
    email_id, action_id = _stage_email(container, active_profile)
    with container.database.session() as session:
        container.approvals.approve(session, action_id, reviewer="Demo Candidate")
        email = session.get(Email, email_id)
        assert email is not None
        email.subject = "Changed after approval"
    fake = FakeMailConnector()

    with pytest.raises(EmailDraftPayloadMismatchError, match="exact packet"):
        _executor(container, fake).execute(action_id, actor="test")
    assert fake.created == []


def test_send_requires_its_own_approval_and_persists_the_sent_message(
    container: Container,
    active_profile: str,
) -> None:
    fake = FakeMailConnector()
    email_id, create_action_id = _materialize_draft(container, active_profile, fake)
    executor = _send_executor(container, fake)
    with container.database.session() as session:
        send_action = executor.request(session, email_id, actor="Demo Candidate")
        send_action_id = send_action.id
        assert send_action.kind is ActionKind.SEND_EMAIL
        assert send_action.status is ActionStatus.PENDING
        assert send_action.payload_json["create_action_id"] == create_action_id
        assert send_action.payload_json["external_draft_id"] == "provider-draft-1"
        assert (
            send_action.payload_json["rfc822_message_id"]
            == "<provider-rfc822-message-id@example.test>"
        )
        assert send_action.payload_json["external_thread_id"] == "provider-thread-1"

    with pytest.raises(EmailSendExecutionError, match="pending"):
        executor.execute(send_action_id, actor="Demo Candidate")
    assert fake.sent == []

    with container.database.session() as session:
        container.approvals.approve(session, send_action_id, reviewer="Demo Candidate")
    first = executor.execute(send_action_id, actor="Demo Candidate")
    second = executor.execute(send_action_id, actor="Demo Candidate")

    assert first.external_message_id == "provider-message-1"
    assert not first.already_executed
    assert second.already_executed
    assert len(fake.sent) == 1
    assert fake.sent[0]["sender"] == "candidate@example.com"
    assert fake.sent[0]["recipient"] == "recipient@example.test"
    with container.database.session() as session:
        action = session.get(ActionRequest, send_action_id)
        email = session.get(Email, email_id)
        assert action is not None and action.status is ActionStatus.EXECUTED
        assert email is not None and email.status is EmailStatus.SENT
        assert email.external_message_id == "provider-message-1"
        assert email.external_thread_id == "provider-thread-1"
        assert email.sent_or_received_at == container.clock.now().replace(tzinfo=None)


def test_send_packet_fails_closed_if_local_email_changes_after_approval(
    container: Container,
    active_profile: str,
) -> None:
    fake = FakeMailConnector()
    email_id, _ = _materialize_draft(container, active_profile, fake)
    executor = _send_executor(container, fake)
    with container.database.session() as session:
        action = executor.request(session, email_id, actor="Demo Candidate")
        send_action_id = action.id
        container.approvals.approve(session, send_action_id, reviewer="Demo Candidate")
        email = session.get(Email, email_id)
        assert email is not None
        email.subject = "Changed after send approval"

    with pytest.raises(EmailSendPayloadMismatchError, match="approved draft packet"):
        executor.execute(send_action_id, actor="Demo Candidate")
    assert fake.sent == []
