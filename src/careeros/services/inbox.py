"""Least-privilege Gmail reply and delivery reconciliation.

The service reads message metadata first and fetches content only for a known CareerOS
thread or a delivery-status notification. Unrelated inbox messages are never opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.connectors.base import MailConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.database.enums import (
    ApplicationStatus,
    ContactDeliveryStatus,
    DeliveryEventKind,
    Direction,
    DocumentKind,
    EmailStatus,
    JobStatus,
    RecipientKind,
    TaskStatus,
)
from careeros.database.models import (
    Application,
    ApplicationTask,
    ApplicationWorkspace,
    Company,
    CompanyAlias,
    CompanyTask,
    Document,
    Email,
    EmailDeliveryEvent,
    EmailRecipient,
    Job,
    PersonContact,
    PersonTask,
    Task,
)
from careeros.database.session import Database
from careeros.domain.records import MailboxMessage
from careeros.services.applications import ApplicationService
from careeros.services.audit import AuditService
from careeros.services.normalization import normalize_email, normalize_text, sha256_text


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive UTC timestamps; compare everything as aware UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


#: Phrases that, in an applicant-tracking-system notice, mean the application is over.
#: Acknowledgements ("we have received your application") contain none of them, so an
#: ATS message without one of these leaves the application open.
REJECTION_PHRASES = (
    "not be moving forward",
    "not to move forward",
    "won't be moving forward",
    "will not be moving forward",
    "decided not to proceed",
    "not to proceed",
    "will not be progressing",
    "not be progressing",
    "move forward with other candidates",
    "moving forward with other candidates",
    "move forward with some other candidates",
    "was not selected",
    "not been selected",
    "unsuccessful",
    "not been successful",
    "not to take your application further",
    "pursue other candidates",
    "other candidates whose experience",
    "not a match at this time",
    "isn't a match",
)
OPEN_APPLICATION_STATUSES = (
    ApplicationStatus.DRAFT,
    ApplicationStatus.APPLIED,
    ApplicationStatus.SCREENING,
    ApplicationStatus.INTERVIEWING,
)


@dataclass(frozen=True)
class InboxReconciliationSummary:
    metadata_seen: int = 0
    messages_read: int = 0
    inbound_created: int = 0
    replies_matched: int = 0
    bounces_recorded: int = 0
    contacts_invalidated: int = 0
    followups_cancelled: int = 0
    notices_read: int = 0
    applications_rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "metadata_seen": self.metadata_seen,
            "messages_read": self.messages_read,
            "inbound_created": self.inbound_created,
            "replies_matched": self.replies_matched,
            "bounces_recorded": self.bounces_recorded,
            "contacts_invalidated": self.contacts_invalidated,
            "followups_cancelled": self.followups_cancelled,
            "notices_read": self.notices_read,
            "applications_rejected": self.applications_rejected,
        }


class InboxReconciliationService:
    """Persist inbound replies and DSNs, then reconcile local workflow state."""

    def __init__(
        self,
        database: Database,
        connectors: ConnectorRegistry,
        clock: Clock,
        audit: AuditService,
    ) -> None:
        self._database = database
        self._connectors = connectors
        self._clock = clock
        self._audit = audit

    def reconcile(self, *, after: datetime, actor: str) -> InboxReconciliationSummary:
        mail = cast(MailConnector, self._connectors.require("mail"))
        ordinary = list(mail.list_messages(after=after))
        bounces = list(mail.find_bounces(after=after))

        with self._database.session() as session:
            known_threads = set(
                session.scalars(
                    select(Email.external_thread_id).where(
                        Email.direction == Direction.OUTBOUND,
                        Email.external_thread_id.is_not(None),
                    )
                ).all()
            )
            existing_ids = set(
                session.scalars(
                    select(Email.external_message_id).where(
                        Email.connector_name == mail.name,
                        Email.external_message_id.is_not(None),
                    )
                ).all()
            )

        bounce_ids = {str(item["id"]) for item in bounces if item.get("id")}
        candidates: dict[str, dict[str, object]] = {}
        for item in (*ordinary, *bounces):
            message_id = item.get("id")
            if not message_id:
                continue
            message_id = str(message_id)
            thread_id = item.get("threadId")
            if message_id in existing_ids:
                continue
            if message_id in bounce_ids or (thread_id and str(thread_id) in known_threads):
                candidates[message_id] = item

        messages = [mail.get_message(message_id) for message_id in candidates]
        created = replies = bounce_count = invalidated = cancelled = 0
        with self._database.session() as session:
            for message in messages:
                outcome = self._record_message(
                    session,
                    message=message,
                    connector_name=mail.name,
                    actor=actor,
                )
                created += outcome[0]
                replies += outcome[1]
                bounce_count += outcome[2]
                invalidated += outcome[3]
                cancelled += outcome[4]

        notices_read, rejected = self._reconcile_application_notices(
            mail,
            after=after,
            actor=actor,
            skip_ids={i for i in existing_ids if i} | set(candidates),
        )

        metadata_ids = {str(item.get("id")) for item in (*ordinary, *bounces) if item.get("id")}
        return InboxReconciliationSummary(
            metadata_seen=len(metadata_ids),
            messages_read=len(messages),
            inbound_created=created,
            replies_matched=replies,
            bounces_recorded=bounce_count,
            contacts_invalidated=invalidated,
            followups_cancelled=cancelled,
            notices_read=notices_read,
            applications_rejected=rejected,
        )

    def _reconcile_application_notices(
        self,
        mail: MailConnector,
        *,
        after: datetime,
        actor: str,
        skip_ids: set[str],
    ) -> tuple[int, int]:
        """Close applications that an ATS notice says are over.

        Only messages the connector identifies as ATS notices are opened. A notice is acted
        on when it names exactly one company holding an open application and contains a
        rejection phrase; anything ambiguous is recorded as inbound mail and left for the
        operator, never guessed.
        """
        notice_ids = [
            str(item["id"])
            for item in mail.find_application_notices(after=after)
            if item.get("id") and str(item["id"]) not in skip_ids
        ]
        if not notice_ids:
            return 0, 0
        rejected = 0
        with self._database.session() as session:
            open_rows = session.execute(
                select(Application, Job, Company)
                .join(Job, Job.id == Application.job_id)
                .join(Company, Company.id == Job.company_id)
                .where(Application.status.in_(OPEN_APPLICATION_STATUSES))
            ).all()
            if not open_rows:
                return 0, 0
            aliases: dict[str, set[str]] = {}
            for alias in session.scalars(select(CompanyAlias)).all():
                aliases.setdefault(alias.company_id, set()).add(alias.normalized_alias)
            names_by_company = {
                company.id: {company.normalized_name, *aliases.get(company.id, set())}
                for _, _, company in open_rows
            }
            read = 0
            for message_id in notice_ids:
                if session.scalar(
                    select(Email).where(
                        Email.connector_name == mail.name,
                        Email.external_message_id == message_id,
                    )
                ):
                    continue
                message = mail.get_message(message_id)
                read += 1
                haystack = normalize_text(f"{message.subject}\n{message.body}")
                hits = [
                    (application, job, company)
                    for application, job, company in open_rows
                    if any(f" {name} " in f" {haystack} " for name in names_by_company[company.id])
                ]
                companies = {company.id for _, _, company in hits}
                if len(companies) == 1 and len(hits) > 1:
                    titled = [row for row in hits if normalize_text(row[1].title) in haystack]
                    hits = titled if len(titled) == 1 else hits
                is_rejection = any(phrase in haystack for phrase in REJECTION_PHRASES)
                matched = hits[0] if len(hits) == 1 else None
                body = self._body_document(session, message)
                inbound = Email(
                    application_id=matched[0].id if matched else None,
                    company_id=matched[2].id if matched else None,
                    connector_name=mail.name,
                    external_message_id=message.external_message_id,
                    external_thread_id=message.external_thread_id,
                    rfc822_message_id=message.rfc822_message_id,
                    direction=Direction.INBOUND,
                    status=EmailStatus.RECEIVED,
                    sender_address=message.sender_address,
                    subject=message.subject,
                    body_document_id=body.id,
                    sent_or_received_at=message.received_at,
                )
                session.add(inbound)
                session.flush()
                self._audit.append(
                    session,
                    actor=actor,
                    event_type="email.application_notice_recorded",
                    aggregate_type="email",
                    aggregate_id=inbound.id,
                    payload={
                        "matched_application": matched[0].id if matched else None,
                        "candidate_companies": len(companies),
                        "rejection_phrase_found": is_rejection,
                    },
                )
                if matched is None or not is_rejection:
                    continue
                application, job, company = matched
                application.status = ApplicationStatus.REJECTED
                application.closed_at = message.received_at
                application.next_followup_at = None
                application.outcome_reason = (
                    f"Rejection notice from {message.sender_address} on "
                    f"{message.received_at.date().isoformat()}: {message.subject}"
                )
                job.status = JobStatus.CLOSED
                if session.get(ApplicationWorkspace, application.id) is not None:
                    workflow = ApplicationService(self._clock, self._audit)
                    workflow.event(
                        session,
                        application.id,
                        "stage_changed",
                        {
                            "status": ApplicationStatus.REJECTED.value,
                            "employer_evidence": application.outcome_reason,
                            "inbound_email_id": inbound.id,
                            "source": "ats_notice",
                            "reflection": "",
                        },
                        actor,
                    )
                    workflow.reconcile(session)
                rejected += 1
                self._audit.append(
                    session,
                    actor=actor,
                    event_type="application.rejected",
                    aggregate_type="application",
                    aggregate_id=application.id,
                    payload={
                        "company": company.canonical_name,
                        "job_title": job.title,
                        "inbound_email_id": inbound.id,
                        "source": "ats_notice",
                    },
                )
        return read, rejected

    def _record_message(
        self,
        session: Session,
        *,
        message: MailboxMessage,
        connector_name: str,
        actor: str,
    ) -> tuple[int, int, int, int, int]:
        existing = session.scalar(
            select(Email).where(
                Email.connector_name == connector_name,
                Email.external_message_id == message.external_message_id,
            )
        )
        if existing is not None:
            return 0, 0, 0, 0, 0

        outbound, matched_by = self._match_outbound(session, message)
        body = self._body_document(session, message)
        inbound = Email(
            application_id=outbound.application_id if outbound else None,
            company_id=outbound.company_id if outbound else None,
            person_id=outbound.person_id if outbound else None,
            connector_name=connector_name,
            external_message_id=message.external_message_id,
            external_thread_id=message.external_thread_id,
            rfc822_message_id=message.rfc822_message_id,
            direction=Direction.INBOUND,
            status=EmailStatus.RECEIVED,
            sender_address=message.sender_address,
            subject=message.subject,
            body_document_id=body.id,
            in_reply_to_email_id=outbound.id if outbound else None,
            sent_or_received_at=message.received_at,
        )
        session.add(inbound)
        session.flush()
        for address in sorted(set(message.recipient_addresses)):
            session.add(
                EmailRecipient(
                    email_id=inbound.id,
                    kind=RecipientKind.TO,
                    address=address,
                )
            )

        self._audit.append(
            session,
            actor=actor,
            event_type="email.inbound_recorded",
            aggregate_type="email",
            aggregate_id=inbound.id,
            payload={
                "connector_name": connector_name,
                "matched_outbound": outbound is not None,
                "matched_by": matched_by,
                "automated": message.automated,
                "is_bounce": message.bounce is not None,
            },
        )

        if message.bounce is not None:
            event = EmailDeliveryEvent(
                outbound_email_id=outbound.id if outbound else None,
                notification_email_id=inbound.id,
                kind=message.bounce.kind,
                recipient_address=message.bounce.recipient_address,
                smtp_status=message.bounce.smtp_status,
                diagnostic=message.bounce.diagnostic,
                matched_by=matched_by,
                occurred_at=message.received_at,
            )
            session.add(event)
            invalidated = self._apply_delivery_status(
                session,
                message=message,
                outbound=outbound,
                actor=actor,
            )
            cancelled = 0
            if outbound is not None and message.bounce.kind is DeliveryEventKind.HARD_BOUNCE:
                outbound.status = EmailStatus.FAILED
                cancelled = self._cancel_followups(session, outbound=outbound, actor=actor)
            self._audit.append(
                session,
                actor=actor,
                event_type="email.delivery_event_recorded",
                aggregate_type="email",
                aggregate_id=outbound.id if outbound else inbound.id,
                payload={
                    "kind": message.bounce.kind.value,
                    "matched_by": matched_by,
                    "contact_invalidated": bool(invalidated),
                },
            )
            return 1, 0, 1, invalidated, cancelled

        # A message older than the outbound it matched is thread history that was already
        # answered, not a reply to the follow-up; it must not cancel anything.
        is_reply = outbound is not None and (
            outbound.sent_or_received_at is None
            or _aware(message.received_at) > _aware(outbound.sent_or_received_at)
        )
        cancelled = (
            self._cancel_followups(session, outbound=outbound, actor=actor)
            if outbound is not None and is_reply
            else 0
        )
        return 1, int(is_reply), 0, 0, cancelled

    @staticmethod
    def _match_outbound(
        session: Session,
        message: MailboxMessage,
    ) -> tuple[Email | None, str | None]:
        filters = [
            Email.direction == Direction.OUTBOUND,
            Email.status.in_([EmailStatus.SENT, EmailStatus.FAILED]),
        ]
        if message.external_thread_id:
            outbound = session.scalar(
                select(Email)
                .where(*filters, Email.external_thread_id == message.external_thread_id)
                .order_by(Email.sent_or_received_at.desc())
            )
            if outbound is not None:
                return outbound, "thread_id"

        referenced = message.in_reply_to
        if message.bounce and message.bounce.original_message_id:
            referenced = message.bounce.original_message_id
        if referenced:
            outbound = session.scalar(
                select(Email)
                .where(*filters, Email.rfc822_message_id == referenced)
                .order_by(Email.sent_or_received_at.desc())
            )
            if outbound is not None:
                return outbound, "rfc822_message_id"

        if message.bounce and message.bounce.recipient_address:
            normalized = normalize_email(message.bounce.recipient_address)
            outbound = session.scalar(
                select(Email)
                .join(EmailRecipient, EmailRecipient.email_id == Email.id)
                .where(
                    *filters,
                    EmailRecipient.kind == RecipientKind.TO,
                    EmailRecipient.address == normalized,
                )
                .order_by(Email.sent_or_received_at.desc())
            )
            if outbound is not None:
                return outbound, "recipient_address"
        return None, None

    def _apply_delivery_status(
        self,
        session: Session,
        *,
        message: MailboxMessage,
        outbound: Email | None,
        actor: str,
    ) -> int:
        bounce = message.bounce
        if bounce is None or not bounce.recipient_address:
            return 0
        contacts = session.scalars(
            select(PersonContact).where(
                PersonContact.contact_type == "email",
                PersonContact.normalized_value == normalize_email(bounce.recipient_address),
            )
        ).all()
        invalidated = 0
        for contact in contacts:
            if bounce.kind is DeliveryEventKind.HARD_BOUNCE:
                contact.delivery_status = ContactDeliveryStatus.INVALID
                contact.verified_at = None
                invalidated += 1
            elif contact.delivery_status is not ContactDeliveryStatus.INVALID:
                contact.delivery_status = ContactDeliveryStatus.RISKY
            contact.delivery_checked_at = self._clock.now()
            contact.delivery_detail = bounce.diagnostic or bounce.smtp_status
            self._audit.append(
                session,
                actor=actor,
                event_type="contact.delivery_status_changed",
                aggregate_type="person_contact",
                aggregate_id=contact.id,
                payload={
                    "delivery_status": contact.delivery_status.value,
                    "outbound_email_id": outbound.id if outbound else None,
                },
            )
        return invalidated

    def _cancel_followups(self, session: Session, *, outbound: Email, actor: str) -> int:
        tasks: dict[str, Task] = {}
        relationships = (
            (ApplicationTask, ApplicationTask.application_id, outbound.application_id),
            (PersonTask, PersonTask.person_id, outbound.person_id),
            (CompanyTask, CompanyTask.company_id, outbound.company_id),
        )
        for model, foreign_key, value in relationships:
            if value is None:
                continue
            rows = session.scalars(
                select(Task)
                .join(model, model.task_id == Task.id)
                .where(
                    foreign_key == value,
                    Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]),
                    Task.title.like("Follow up:%"),
                )
            ).all()
            tasks.update((task.id, task) for task in rows)
        for task in tasks.values():
            task.status = TaskStatus.CANCELLED
            self._audit.append(
                session,
                actor=actor,
                event_type="task.cancelled_after_email_event",
                aggregate_type="task",
                aggregate_id=task.id,
                payload={"outbound_email_id": outbound.id},
            )
        return len(tasks)

    @staticmethod
    def _body_document(session: Session, message: MailboxMessage) -> Document:
        digest = sha256_text(message.body)
        existing = session.scalar(select(Document).where(Document.sha256 == digest))
        if existing is not None:
            return existing
        document = Document(
            kind=DocumentKind.EMAIL,
            title=f"Inbound email: {message.subject}"[:500],
            media_type="text/plain",
            content_text=message.body,
            sha256=digest,
            source_url=(
                f"https://mail.google.com/mail/u/0/#inbox/{message.external_thread_id}"
                if message.external_thread_id
                else None
            ),
            captured_at=message.received_at,
        )
        session.add(document)
        session.flush()
        return document
