"""Exact-payload execution for approved external email drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.connectors.base import MailConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.database.enums import ActionKind, ActionStatus, RecipientKind
from careeros.database.models import (
    ActionRequest,
    Document,
    Email,
    EmailEvidence,
    EmailRecipient,
)
from careeros.database.session import Database
from careeros.services.audit import AuditService
from careeros.services.contact_policy import ContactPolicy
from careeros.services.normalization import hash_payload, sha256_text

PAYLOAD_VERSION = 1
GMAIL_CONNECTOR_NAMES = frozenset({"gmail", "google-workspace-gmail"})


class EmailDraftExecutionError(RuntimeError):
    """Raised when an action cannot safely be materialized as a provider draft."""


class EmailDraftPayloadMismatchError(EmailDraftExecutionError):
    """Raised when current database state differs from the approved packet."""


@dataclass(frozen=True)
class EmailDraftPacket:
    email_id: str
    sender: str
    recipient: str
    subject: str
    body: str
    approval_payload: dict[str, Any]
    #: RFC 822 Message-ID of the sent message this draft replies to, when it is a reply.
    in_reply_to_rfc822_message_id: str | None = None
    #: Provider thread the reply belongs in, when it is a reply.
    external_thread_id: str | None = None


@dataclass(frozen=True)
class EmailDraftExecutionResult:
    email_id: str
    external_draft_id: str
    already_executed: bool


def load_email_draft_packet(session: Session, email_id: str) -> EmailDraftPacket:
    """Load and fingerprint every provider-visible field plus its evidence lineage."""
    email = session.get(Email, email_id)
    if email is None:
        raise EmailDraftExecutionError(f"email not found: {email_id}")
    body_document = session.get(Document, email.body_document_id)
    if body_document is None or body_document.content_text is None:
        raise EmailDraftExecutionError(f"email body document is missing text: {email_id}")
    body_sha256 = sha256_text(body_document.content_text)
    if body_document.sha256 != body_sha256:
        raise EmailDraftPayloadMismatchError(
            f"email body digest does not match document {body_document.id}"
        )

    recipients = session.scalars(
        select(EmailRecipient)
        .where(
            EmailRecipient.email_id == email.id,
            EmailRecipient.kind == RecipientKind.TO,
        )
        .order_by(EmailRecipient.address)
    ).all()
    if len(recipients) != 1:
        raise EmailDraftExecutionError(
            f"email {email.id} must have exactly one TO recipient; found {len(recipients)}"
        )
    recipient = recipients[0].address

    evidence_rows = session.execute(
        select(EmailEvidence, Document)
        .join(Document, Document.id == EmailEvidence.document_id)
        .where(EmailEvidence.email_id == email.id)
        .order_by(Document.id)
    ).all()
    evidence: list[dict[str, str | None]] = []
    for _, document in evidence_rows:
        if not document.sha256:
            raise EmailDraftExecutionError(
                f"evidence document has no content digest: {document.id}"
            )
        evidence.append(
            {
                "document_id": document.id,
                "sha256": document.sha256,
                "source_url": document.source_url,
            }
        )

    approval_payload: dict[str, Any] = {
        "version": PAYLOAD_VERSION,
        "operation": "create_email_draft",
        "email_id": email.id,
        "sender": email.sender_address,
        "recipient": recipient,
        "subject": email.subject,
        "body_document_id": body_document.id,
        "body_sha256": body_sha256,
        "evidence": evidence,
    }
    in_reply_to: str | None = None
    thread_id: str | None = None
    if email.in_reply_to_email_id:
        parent = session.get(Email, email.in_reply_to_email_id)
        if parent is None:
            raise EmailDraftExecutionError(
                f"email {email.id} replies to a missing email {email.in_reply_to_email_id}"
            )
        if not parent.rfc822_message_id or not parent.external_thread_id:
            raise EmailDraftExecutionError(
                f"email {email.id} replies to {parent.id}, which has not been sent yet"
            )
        in_reply_to = parent.rfc822_message_id
        thread_id = parent.external_thread_id
        # Only replies carry these keys, so approval hashes of ordinary drafts are unchanged.
        approval_payload["in_reply_to_rfc822_message_id"] = in_reply_to
        approval_payload["external_thread_id"] = thread_id
    return EmailDraftPacket(
        email_id=email.id,
        sender=email.sender_address,
        recipient=recipient,
        subject=email.subject,
        body=body_document.content_text,
        approval_payload=approval_payload,
        in_reply_to_rfc822_message_id=in_reply_to,
        external_thread_id=thread_id,
    )


class EmailDraftExecutor:
    """Materialize one approved packet in Gmail without any ability to send it."""

    def __init__(
        self,
        database: Database,
        connectors: ConnectorRegistry,
        clock: Clock,
        audit: AuditService,
        *,
        contact_policy: ContactPolicy | None = None,
    ) -> None:
        self._database = database
        self._connectors = connectors
        self._clock = clock
        self._audit = audit
        self._contact_policy = contact_policy or ContactPolicy()

    def execute(
        self,
        action_id: str,
        *,
        actor: str,
        resume: bool = False,
    ) -> EmailDraftExecutionResult:
        """Create or recover the provider draft for one immutable approved action."""
        with self._database.session() as session:
            action, packet = self._validated_action(session, action_id)
            existing_id = self._existing_result(action, session, packet)
            if existing_id is not None:
                return EmailDraftExecutionResult(packet.email_id, existing_id, True)
            self._require_executable_status(action, resume=resume)

        mail = cast(MailConnector, self._connectors.require("mail"))

        with self._database.session() as session:
            action, packet = self._validated_action(session, action_id)
            self._require_executable_status(action, resume=resume)
            retrying = action.status in {ActionStatus.EXECUTING, ActionStatus.FAILED}
            action.status = ActionStatus.EXECUTING
            action.error = None
            self._audit.append(
                session,
                actor=actor,
                event_type=("action.execution_resumed" if retrying else "action.execution_started"),
                aggregate_type="action_request",
                aggregate_id=action.id,
                action_request_id=action.id,
                payload={
                    "operation": "create_email_draft",
                    "payload_sha256": action.payload_sha256,
                },
            )

        try:
            external_id = mail.create_draft(
                sender=packet.sender,
                recipient=packet.recipient,
                subject=packet.subject,
                body=packet.body,
                idempotency_key=action.idempotency_key,
                in_reply_to=packet.in_reply_to_rfc822_message_id,
                thread_id=packet.external_thread_id,
            )
        except Exception as error:
            self._record_failure(action_id, actor=actor, error=error)
            raise

        with self._database.session() as session:
            action = self._require_action(session, action_id)
            if action.status is not ActionStatus.EXECUTING:
                raise EmailDraftExecutionError(
                    f"action {action.id} left executing state before completion"
                )
            email = session.get(Email, packet.email_id)
            if email is None:
                raise EmailDraftExecutionError(f"email not found: {packet.email_id}")
            if email.external_message_id not in {None, external_id}:
                raise EmailDraftExecutionError(
                    f"email {email.id} is already linked to another external draft"
                )
            email.external_message_id = external_id
            if packet.external_thread_id and not email.external_thread_id:
                email.external_thread_id = packet.external_thread_id
            action.status = ActionStatus.EXECUTED
            action.executed_at = self._clock.now()
            action.error = None
            self._audit.append(
                session,
                actor=actor,
                event_type="email.external_draft_created",
                aggregate_type="email",
                aggregate_id=email.id,
                action_request_id=action.id,
                payload={
                    "connector_name": mail.name,
                    "external_draft_id": external_id,
                },
            )
            self._audit.append(
                session,
                actor=actor,
                event_type="action.executed",
                aggregate_type="action_request",
                aggregate_id=action.id,
                action_request_id=action.id,
                payload={
                    "operation": "create_email_draft",
                    "external_draft_id": external_id,
                },
            )
        return EmailDraftExecutionResult(packet.email_id, external_id, False)

    def _validated_action(
        self,
        session: Session,
        action_id: str,
    ) -> tuple[ActionRequest, EmailDraftPacket]:
        action = self._require_action(session, action_id)
        if action.kind is not ActionKind.CREATE_EMAIL_DRAFT:
            raise EmailDraftExecutionError(
                f"action {action.id} is {action.kind.value}, not create_email_draft"
            )
        if action.connector_name not in GMAIL_CONNECTOR_NAMES:
            raise EmailDraftExecutionError(
                f"unsupported email-draft connector: {action.connector_name}"
            )
        if hash_payload(action.payload_json) != action.payload_sha256:
            raise EmailDraftPayloadMismatchError(
                f"action {action.id} payload no longer matches its stored approval hash"
            )
        email_id = action.payload_json.get("email_id")
        if not isinstance(email_id, str) or not email_id:
            raise EmailDraftExecutionError(f"action {action.id} has no email_id")
        packet = load_email_draft_packet(session, email_id)
        if packet.approval_payload != action.payload_json:
            raise EmailDraftPayloadMismatchError(
                f"email {email_id} no longer matches the exact packet submitted for approval"
            )
        # An exclusion added after approval still stops the provider call.
        self._contact_policy.check_email(session, email_id)
        return action, packet

    @staticmethod
    def _existing_result(
        action: ActionRequest,
        session: Session,
        packet: EmailDraftPacket,
    ) -> str | None:
        if action.status is not ActionStatus.EXECUTED:
            return None
        email = session.get(Email, packet.email_id)
        if email is None or not email.external_message_id:
            raise EmailDraftExecutionError(
                f"executed action {action.id} has no stored external draft id"
            )
        return email.external_message_id

    @staticmethod
    def _require_executable_status(action: ActionRequest, *, resume: bool) -> None:
        if action.status is ActionStatus.PENDING:
            raise EmailDraftExecutionError(
                f"action {action.id} is pending; review and approve it before execution"
            )
        if action.status in {ActionStatus.EXECUTING, ActionStatus.FAILED}:
            if resume:
                return
            raise EmailDraftExecutionError(
                f"action {action.id} is {action.status.value}; use --resume to retry safely"
            )
        if action.status is not ActionStatus.APPROVED:
            raise EmailDraftExecutionError(
                f"action {action.id} cannot execute from {action.status.value}"
            )

    def _record_failure(self, action_id: str, *, actor: str, error: Exception) -> None:
        detail = f"{type(error).__name__}: {error}"[:2000]
        with self._database.session() as session:
            action = self._require_action(session, action_id)
            if action.status is not ActionStatus.EXECUTING:
                return
            action.status = ActionStatus.FAILED
            action.error = detail
            self._audit.append(
                session,
                actor=actor,
                event_type="action.execution_failed",
                aggregate_type="action_request",
                aggregate_id=action.id,
                action_request_id=action.id,
                payload={"error_type": type(error).__name__},
            )

    @staticmethod
    def _require_action(session: Session, action_id: str) -> ActionRequest:
        action = session.get(ActionRequest, action_id)
        if action is None:
            raise LookupError(f"action request not found: {action_id}")
        return action
