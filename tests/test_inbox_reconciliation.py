from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.enums import (
    ContactDeliveryStatus,
    DeliveryEventKind,
    Direction,
    DocumentKind,
    EmailStatus,
    RecipientKind,
    TaskStatus,
)
from careeros.database.models import (
    Company,
    Document,
    Email,
    EmailDeliveryEvent,
    EmailRecipient,
    PersonContact,
    PersonTask,
    Task,
)
from careeros.domain.records import BounceReport, MailboxMessage
from careeros.services.inbox import InboxReconciliationService
from careeros.services.normalization import normalize_text, sha256_text


class FakeInboxConnector:
    name = "fake-inbox"

    def __init__(
        self,
        *,
        ordinary: list[dict[str, Any]],
        bounces: list[dict[str, Any]],
        messages: dict[str, MailboxMessage],
    ) -> None:
        self.ordinary = ordinary
        self.bounces = bounces
        self.messages = messages
        self.read: list[str] = []

    def healthcheck(self) -> None:
        return None

    def list_messages(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return self.ordinary

    def find_bounces(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return self.bounces

    def find_application_notices(self, *, after: datetime) -> list[dict[str, Any]]:
        return []

    def get_message(self, external_message_id: str) -> MailboxMessage:
        self.read.append(external_message_id)
        return self.messages[external_message_id]


def _outbound(container: Container) -> tuple[str, str, str]:
    with container.database.session() as session:
        company = Company(
            canonical_name="Example Capital",
            normalized_name=normalize_text("Example Capital"),
            domain="example.test",
        )
        session.add(company)
        session.flush()
        person = container.people.add_public_person(
            session,
            company_id=company.id,
            full_name="Sample Contact",
            title="Head of Quant",
            influence_probability=0.9,
            confidence=0.8,
            linkedin_url=None,
            email="recipient@example.test",
            email_source_url="https://example.test/team/contact",
            actor="test",
        )
        body_text = "Original outbound body"
        body = Document(
            kind=DocumentKind.EMAIL,
            title="Original",
            media_type="text/plain",
            content_text=body_text,
            sha256=sha256_text(body_text),
        )
        session.add(body)
        session.flush()
        email = Email(
            company_id=company.id,
            person_id=person.id,
            connector_name="gmail",
            external_message_id="gmail-sent-1",
            external_thread_id="thread-1",
            rfc822_message_id="<original-1@example.com>",
            direction=Direction.OUTBOUND,
            status=EmailStatus.SENT,
            sender_address="candidate@example.com",
            subject="Quant role",
            body_document_id=body.id,
            sent_or_received_at=container.clock.now() - timedelta(days=1),
        )
        session.add(email)
        session.flush()
        session.add(
            EmailRecipient(
                email_id=email.id,
                kind=RecipientKind.TO,
                address="recipient@example.test",
                person_id=person.id,
            )
        )
        task = Task(
            title="Follow up: Example Capital — Quant role",
            status=TaskStatus.OPEN,
            priority=90,
            deduplication_key="follow-up-example",
        )
        session.add(task)
        session.flush()
        session.add(PersonTask(person_id=person.id, task_id=task.id))
        return email.id, person.id, task.id


def _service(container: Container, fake: FakeInboxConnector) -> InboxReconciliationService:
    registry = ConnectorRegistry()
    registry.register("mail", fake)
    return InboxReconciliationService(
        container.database,
        registry,
        container.clock,
        container.audit,
    )


def test_reply_reconciliation_reads_only_known_threads_and_cancels_followup(
    container: Container,
) -> None:
    outbound_id, _, task_id = _outbound(container)
    reply = MailboxMessage(
        external_message_id="reply-1",
        external_thread_id="thread-1",
        rfc822_message_id="<reply-1@example.test>",
        in_reply_to="<original-1@example.com>",
        sender_address="recipient@example.test",
        recipient_addresses=("candidate@example.com",),
        subject="Re: Quant role",
        body="Thanks, let us speak.",
        received_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    fake = FakeInboxConnector(
        ordinary=[
            {"id": "reply-1", "threadId": "thread-1"},
            {"id": "private-unrelated", "threadId": "another-thread"},
        ],
        bounces=[],
        messages={"reply-1": reply},
    )

    summary = _service(container, fake).reconcile(
        after=datetime(2026, 8, 19, tzinfo=UTC),
        actor="test",
    )

    assert fake.read == ["reply-1"]
    assert summary.inbound_created == 1
    assert summary.replies_matched == 1
    assert summary.followups_cancelled == 1
    with container.database.session() as session:
        inbound = session.scalar(select(Email).where(Email.external_message_id == "reply-1"))
        task = session.get(Task, task_id)
        assert inbound is not None and inbound.in_reply_to_email_id == outbound_id
        assert task is not None and task.status is TaskStatus.CANCELLED


def test_hard_bounce_marks_contact_invalid_and_outbound_failed(container: Container) -> None:
    outbound_id, person_id, task_id = _outbound(container)
    notice = MailboxMessage(
        external_message_id="dsn-1",
        external_thread_id="dsn-thread",
        rfc822_message_id="<dsn-1@example.test>",
        sender_address="mailer-daemon@example.net",
        recipient_addresses=("candidate@example.com",),
        subject="Delivery failure",
        body="550 user unknown",
        received_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        automated=True,
        bounce=BounceReport(
            kind=DeliveryEventKind.HARD_BOUNCE,
            recipient_address="recipient@example.test",
            smtp_status="5.1.1",
            diagnostic="550 user unknown",
            original_message_id="<original-1@example.com>",
        ),
    )
    fake = FakeInboxConnector(
        ordinary=[],
        bounces=[{"id": "dsn-1", "threadId": "dsn-thread"}],
        messages={"dsn-1": notice},
    )

    summary = _service(container, fake).reconcile(
        after=datetime(2026, 8, 19, tzinfo=UTC),
        actor="test",
    )

    assert summary.bounces_recorded == 1
    assert summary.contacts_invalidated == 1
    with container.database.session() as session:
        outbound = session.get(Email, outbound_id)
        contact = session.scalar(select(PersonContact).where(PersonContact.person_id == person_id))
        event = session.scalar(select(EmailDeliveryEvent))
        task = session.get(Task, task_id)
        assert outbound is not None and outbound.status is EmailStatus.FAILED
        assert contact is not None and contact.delivery_status is ContactDeliveryStatus.INVALID
        assert event is not None and event.kind is DeliveryEventKind.HARD_BOUNCE
        assert event.matched_by == "rfc822_message_id"
        assert task is not None and task.status is TaskStatus.CANCELLED
