"""ATS rejection notices close the matching application and nothing else."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.enums import ApplicationStatus, JobStatus
from careeros.database.models import Application, Company, Email, Job
from careeros.domain.records import MailboxMessage
from careeros.services.inbox import InboxReconciliationService
from careeros.services.normalization import normalize_text

RECEIVED = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)


class NoticeConnector:
    name = "fake-inbox"

    def __init__(self, notices: dict[str, MailboxMessage]) -> None:
        self.notices = notices
        self.read: list[str] = []

    def healthcheck(self) -> None:
        return None

    def list_messages(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return []

    def find_bounces(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return []

    def find_application_notices(self, *, after: datetime) -> list[dict[str, Any]]:
        del after
        return [{"id": key, "threadId": f"t-{key}"} for key in self.notices]

    def get_message(self, external_message_id: str) -> MailboxMessage:
        self.read.append(external_message_id)
        return self.notices[external_message_id]


def _application(container: Container, company_name: str, title: str) -> str:
    with container.database.session() as session:
        company = Company(canonical_name=company_name, normalized_name=normalize_text(company_name))
        session.add(company)
        session.flush()
        job = Job(
            company_id=company.id,
            canonical_fingerprint=f"fp-{normalize_text(company_name)}-{normalize_text(title)}",
            title=title,
            normalized_title=normalize_text(title),
            status=JobStatus.APPLIED,
            first_seen_at=RECEIVED,
            last_seen_at=RECEIVED,
        )
        session.add(job)
        session.flush()
        application = Application(
            job_id=job.id, status=ApplicationStatus.APPLIED, applied_at=RECEIVED
        )
        session.add(application)
        session.flush()
        return application.id


def _notice(message_id: str, subject: str, body: str) -> MailboxMessage:
    return MailboxMessage(
        external_message_id=message_id,
        external_thread_id=f"t-{message_id}",
        sender_address="no-reply@ashbyhq.test",
        recipient_addresses=("candidate@example.com",),
        subject=subject,
        body=body,
        received_at=RECEIVED,
        automated=True,
    )


def _service(container: Container, fake: NoticeConnector) -> InboxReconciliationService:
    registry = ConnectorRegistry()
    registry.register("mail", fake)
    return InboxReconciliationService(
        container.database, registry, container.clock, container.audit
    )


def test_rejection_notice_closes_the_named_application(container: Container) -> None:
    application_id = _application(container, "Example Health", "Operations Analyst")
    fake = NoticeConnector(
        {
            "n1": _notice(
                "n1",
                "Thanks for your interest in Example Health, Demo Candidate",
                "This is a fictional rejection: we will not be progressing this application.",
            )
        }
    )
    summary = _service(container, fake).reconcile(after=RECEIVED, actor="test")
    assert summary.notices_read == 1
    assert summary.applications_rejected == 1
    with container.database.session() as session:
        application = session.get(Application, application_id)
        assert application is not None
        assert application.status is ApplicationStatus.REJECTED
        assert application.closed_at is not None
        assert application.closed_at.replace(tzinfo=UTC) == RECEIVED
        assert application.next_followup_at is None
        job = session.get(Job, application.job_id)
        assert job is not None and job.status is JobStatus.CLOSED
        inbound = session.query(Email).filter(Email.external_message_id == "n1").one()
        assert inbound.application_id == application_id


def test_acknowledgement_leaves_the_application_open(container: Container) -> None:
    application_id = _application(container, "Example Payments", "Account Manager")
    fake = NoticeConnector(
        {
            "n2": _notice(
                "n2",
                "Thank you for your application to Example Payments",
                "Your application has been received and is being reviewed.",
            )
        }
    )
    summary = _service(container, fake).reconcile(after=RECEIVED, actor="test")
    assert summary.notices_read == 1
    assert summary.applications_rejected == 0
    with container.database.session() as session:
        application = session.get(Application, application_id)
        assert application is not None
        assert application.status is ApplicationStatus.APPLIED


def test_notice_naming_two_companies_is_recorded_but_not_acted_on(
    container: Container,
) -> None:
    first = _application(container, "Example Alpha", "Marketing Manager")
    second = _application(container, "Example Beta", "Head of Marketing")
    fake = NoticeConnector(
        {
            "n3": _notice(
                "n3",
                "Your application",
                "Fictional notice: Example Alpha and Example Beta decided not to proceed.",
            )
        }
    )
    summary = _service(container, fake).reconcile(after=RECEIVED, actor="test")
    assert summary.applications_rejected == 0
    with container.database.session() as session:
        for application_id in (first, second):
            application = session.get(Application, application_id)
            assert application is not None
            assert application.status is ApplicationStatus.APPLIED
        assert session.query(Email).filter(Email.external_message_id == "n3").count() == 1
    summary_again = _service(container, fake).reconcile(after=RECEIVED, actor="test")
    assert summary_again.notices_read == 0


def test_thread_history_older_than_the_outbound_does_not_cancel_followups(
    container: Container,
) -> None:
    from datetime import timedelta

    from careeros.database.enums import Direction, DocumentKind, EmailStatus, TaskStatus
    from careeros.database.models import CompanyTask, Document, Task
    from careeros.services.normalization import sha256_text

    with container.database.session() as session:
        company = Company(canonical_name="Example Capital", normalized_name="example-capital")
        session.add(company)
        session.flush()
        body = Document(
            kind=DocumentKind.EMAIL,
            title="out",
            media_type="text/plain",
            content_text="Status check.",
            sha256=sha256_text("Status check."),
        )
        session.add(body)
        session.flush()
        outbound = Email(
            company_id=company.id,
            connector_name="fake-inbox",
            external_message_id="out-1",
            external_thread_id="t-old",
            rfc822_message_id="<out-1@careeros.local>",
            direction=Direction.OUTBOUND,
            status=EmailStatus.SENT,
            sender_address="candidate@example.com",
            subject="Status check",
            body_document_id=body.id,
            sent_or_received_at=RECEIVED + timedelta(days=20),
        )
        session.add(outbound)
        session.flush()
        task = Task(
            title="Follow up: Example Capital",
            status=TaskStatus.OPEN,
            priority=80,
            deduplication_key="followup:test:example-capital",
        )
        session.add(task)
        session.flush()
        session.add(CompanyTask(company_id=company.id, task_id=task.id))
        task_id = task.id

    class HistoryConnector(NoticeConnector):
        def list_messages(self, *, after: datetime) -> list[dict[str, Any]]:
            del after
            return [{"id": "old-reply", "threadId": "t-old"}]

        def find_application_notices(self, *, after: datetime) -> list[dict[str, Any]]:
            del after
            return []

    old_reply = MailboxMessage(
        external_message_id="old-reply",
        external_thread_id="t-old",
        sender_address="recipient.other@example.test",
        subject="RE: Status check",
        body="Please attach the demonstration document.",
        received_at=RECEIVED,
    )
    fake = HistoryConnector({"old-reply": old_reply})
    summary = _service(container, fake).reconcile(after=RECEIVED, actor="test")
    assert summary.inbound_created == 1
    assert summary.replies_matched == 0
    assert summary.followups_cancelled == 0
    with container.database.session() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.status is TaskStatus.OPEN
