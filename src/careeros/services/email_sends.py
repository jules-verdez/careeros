"""Exact-payload approval and execution for sending an existing Gmail draft."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.connectors.base import MailConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.database.enums import ActionKind, ActionStatus, EmailStatus
from careeros.database.models import ActionRequest, Email
from careeros.database.session import Database
from careeros.services.approvals import ApprovalService
from careeros.services.audit import AuditService
from careeros.services.contact_policy import ContactPolicy
from careeros.services.email_drafts import (
    GMAIL_CONNECTOR_NAMES,
    EmailDraftPacket,
    load_email_draft_packet,
)
from careeros.services.normalization import hash_payload

SEND_PAYLOAD_VERSION = 3


class EmailSendExecutionError(RuntimeError):
    """Raised when an email cannot safely move from an approved draft to sent."""


class EmailSendPayloadMismatchError(EmailSendExecutionError):
    """Raised when local state differs from the immutable send packet."""


@dataclass(frozen=True)
class EmailSendPacket:
    email: EmailDraftPacket
    external_draft_id: str
    create_action_id: str
    draft_idempotency_key: str
    rfc822_message_id: str
    external_thread_id: str
    approval_payload: dict[str, Any]


@dataclass(frozen=True)
class EmailSendExecutionResult:
    email_id: str
    external_message_id: str
    already_executed: bool


def load_email_send_packet(
    session: Session,
    email_id: str,
    *,
    rfc822_message_id: str,
    external_thread_id: str,
) -> EmailSendPacket:
    """Freeze the approved email fields together with the provider draft identity."""
    email_row = session.get(Email, email_id)
    if email_row is None:
        raise EmailSendExecutionError(f"email not found: {email_id}")
    if not email_row.external_message_id:
        raise EmailSendExecutionError(
            f"email {email_id} has no Gmail draft; execute its draft action first"
        )
    if email_row.status is EmailStatus.SENT:
        raise EmailSendExecutionError(f"email {email_id} is already sent")

    email = load_email_draft_packet(session, email_id)
    create_action = _executed_create_action(session, email)
    approval_payload = {
        **email.approval_payload,
        "version": SEND_PAYLOAD_VERSION,
        "operation": "send_email",
        "external_draft_id": email_row.external_message_id,
        "create_action_id": create_action.id,
        "draft_idempotency_key": create_action.idempotency_key,
        "rfc822_message_id": rfc822_message_id,
        "external_thread_id": external_thread_id,
    }
    return EmailSendPacket(
        email=email,
        external_draft_id=email_row.external_message_id,
        create_action_id=create_action.id,
        draft_idempotency_key=create_action.idempotency_key,
        rfc822_message_id=rfc822_message_id,
        external_thread_id=external_thread_id,
        approval_payload=approval_payload,
    )


def _executed_create_action(session: Session, packet: EmailDraftPacket) -> ActionRequest:
    actions = session.scalars(
        select(ActionRequest)
        .where(
            ActionRequest.kind == ActionKind.CREATE_EMAIL_DRAFT,
            ActionRequest.status == ActionStatus.EXECUTED,
        )
        .order_by(ActionRequest.executed_at.desc())
    ).all()
    for action in actions:
        if action.payload_json.get("email_id") != packet.email_id:
            continue
        if hash_payload(action.payload_json) != action.payload_sha256:
            raise EmailSendPayloadMismatchError(
                f"draft action {action.id} no longer matches its stored approval hash"
            )
        if action.payload_json != packet.approval_payload:
            raise EmailSendPayloadMismatchError(
                f"email {packet.email_id} no longer matches its approved draft packet"
            )
        return action
    raise EmailSendExecutionError(
        f"email {packet.email_id} has no executed exact-payload draft action"
    )


class EmailSendExecutor:
    """Request and execute a send that is distinct from draft approval."""

    def __init__(
        self,
        database: Database,
        connectors: ConnectorRegistry,
        clock: Clock,
        audit: AuditService,
        approvals: ApprovalService,
        *,
        contact_policy: ContactPolicy | None = None,
    ) -> None:
        self._database = database
        self._connectors = connectors
        self._clock = clock
        self._audit = audit
        self._approvals = approvals
        self._contact_policy = contact_policy or ContactPolicy()

    def request(self, session: Session, email_id: str, *, actor: str) -> ActionRequest:
        """Stage a separate send approval for an already materialized Gmail draft."""
        email = session.get(Email, email_id)
        if email is None or not email.external_message_id:
            raise EmailSendExecutionError(
                f"email {email_id} has no Gmail draft; execute its draft action first"
            )
        self._contact_policy.check_email(session, email_id)
        mail = cast(MailConnector, self._connectors.require("mail"))
        rfc822_message_id, external_thread_id = mail.draft_identity(email.external_message_id)
        packet = load_email_send_packet(
            session,
            email_id,
            rfc822_message_id=rfc822_message_id,
            external_thread_id=external_thread_id,
        )
        # Persist the two provider identities as soon as Gmail exposes them. A delivery
        # notification can arrive after send but before a later reconciliation pass, and
        # DSNs refer to the RFC Message-ID rather than Gmail's API message id.
        email.rfc822_message_id = rfc822_message_id
        email.external_thread_id = external_thread_id
        idempotency_key = (
            f"send-email:{email_id}:{packet.external_draft_id}:"
            f"{packet.email.approval_payload['body_sha256']}:"
            f"{packet.rfc822_message_id}:{packet.external_thread_id}"
        )
        existing = session.scalar(
            select(ActionRequest).where(ActionRequest.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        action = self._approvals.request(
            session,
            kind=ActionKind.SEND_EMAIL,
            summary=(
                f"Send email from {packet.email.sender} to {packet.email.recipient}: "
                f"{packet.email.subject}"
            ),
            connector_name="gmail",
            payload=packet.approval_payload,
            idempotency_key=idempotency_key,
            requested_by=actor,
        )
        self._audit.append(
            session,
            actor=actor,
            event_type="email.send_staged",
            aggregate_type="email",
            aggregate_id=email_id,
            action_request_id=action.id,
            payload={
                "external_draft_id": packet.external_draft_id,
                "payload_sha256": action.payload_sha256,
            },
        )
        return action

    def execute(
        self,
        action_id: str,
        *,
        actor: str,
        resume: bool = False,
    ) -> EmailSendExecutionResult:
        """Send an unchanged Gmail draft after its own exact-payload approval."""
        with self._database.session() as session:
            action = self._require_action(session, action_id)
            existing_id = self._existing_result(action, session)
            if existing_id is not None:
                email_id = action.payload_json.get("email_id")
                if not isinstance(email_id, str) or not email_id:
                    raise EmailSendExecutionError(f"action {action.id} has no email_id")
                return EmailSendExecutionResult(email_id, existing_id, True)
            action, packet = self._validated_action(session, action_id)
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
                payload={"operation": "send_email", "payload_sha256": action.payload_sha256},
            )

        try:
            external_id = mail.send_draft(
                packet.external_draft_id,
                sender=packet.email.sender,
                recipient=packet.email.recipient,
                subject=packet.email.subject,
                body=packet.email.body,
                idempotency_key=packet.draft_idempotency_key,
                rfc822_message_id=packet.rfc822_message_id,
                external_thread_id=packet.external_thread_id,
            )
        except Exception as error:
            self._record_failure(action_id, actor=actor, error=error)
            raise

        with self._database.session() as session:
            action = self._require_action(session, action_id)
            if action.status is not ActionStatus.EXECUTING:
                raise EmailSendExecutionError(
                    f"action {action.id} left executing state before completion"
                )
            email = session.get(Email, packet.email.email_id)
            if email is None:
                raise EmailSendExecutionError(f"email not found: {packet.email.email_id}")
            if email.external_message_id != packet.external_draft_id:
                raise EmailSendPayloadMismatchError(
                    f"email {email.id} is no longer linked to the approved Gmail draft"
                )
            email.external_message_id = external_id
            email.external_thread_id = packet.external_thread_id
            email.rfc822_message_id = packet.rfc822_message_id
            email.status = EmailStatus.SENT
            email.sent_or_received_at = self._clock.now()
            action.status = ActionStatus.EXECUTED
            action.executed_at = self._clock.now()
            action.error = None
            self._audit.append(
                session,
                actor=actor,
                event_type="email.sent",
                aggregate_type="email",
                aggregate_id=email.id,
                action_request_id=action.id,
                payload={
                    "connector_name": mail.name,
                    "external_message_id": external_id,
                },
            )
            self._audit.append(
                session,
                actor=actor,
                event_type="action.executed",
                aggregate_type="action_request",
                aggregate_id=action.id,
                action_request_id=action.id,
                payload={"operation": "send_email", "external_message_id": external_id},
            )
        return EmailSendExecutionResult(packet.email.email_id, external_id, False)

    def _validated_action(
        self,
        session: Session,
        action_id: str,
    ) -> tuple[ActionRequest, EmailSendPacket]:
        action = self._require_action(session, action_id)
        if action.kind is not ActionKind.SEND_EMAIL:
            raise EmailSendExecutionError(
                f"action {action.id} is {action.kind.value}, not send_email"
            )
        if action.connector_name not in GMAIL_CONNECTOR_NAMES:
            raise EmailSendExecutionError(
                f"unsupported email-send connector: {action.connector_name}"
            )
        if hash_payload(action.payload_json) != action.payload_sha256:
            raise EmailSendPayloadMismatchError(
                f"action {action.id} payload no longer matches its stored approval hash"
            )
        if action.payload_json.get("version") != SEND_PAYLOAD_VERSION:
            raise EmailSendExecutionError(f"action {action.id} has an unsupported payload version")
        if action.payload_json.get("operation") != "send_email":
            raise EmailSendExecutionError(f"action {action.id} is not an exact-payload send action")
        email_id = action.payload_json.get("email_id")
        if not isinstance(email_id, str) or not email_id:
            raise EmailSendExecutionError(f"action {action.id} has no email_id")
        rfc822_message_id = action.payload_json.get("rfc822_message_id")
        if not isinstance(rfc822_message_id, str) or not rfc822_message_id:
            raise EmailSendExecutionError(f"action {action.id} has no RFC Message-ID")
        external_thread_id = action.payload_json.get("external_thread_id")
        if not isinstance(external_thread_id, str) or not external_thread_id:
            raise EmailSendExecutionError(f"action {action.id} has no external thread ID")
        packet = load_email_send_packet(
            session,
            email_id,
            rfc822_message_id=rfc822_message_id,
            external_thread_id=external_thread_id,
        )
        if packet.approval_payload != action.payload_json:
            raise EmailSendPayloadMismatchError(
                f"email {email_id} no longer matches the exact send packet submitted for approval"
            )
        # An exclusion added after approval still stops the send.
        self._contact_policy.check_email(session, email_id)
        return action, packet

    @staticmethod
    def _existing_result(
        action: ActionRequest,
        session: Session,
    ) -> str | None:
        if action.status is not ActionStatus.EXECUTED:
            return None
        email_id = action.payload_json.get("email_id")
        if not isinstance(email_id, str) or not email_id:
            raise EmailSendExecutionError(f"executed action {action.id} has no email_id")
        email = session.get(Email, email_id)
        if email is None or email.status is not EmailStatus.SENT or not email.external_message_id:
            raise EmailSendExecutionError(
                f"executed action {action.id} has no stored sent-message id"
            )
        return email.external_message_id

    @staticmethod
    def _require_executable_status(action: ActionRequest, *, resume: bool) -> None:
        if action.status is ActionStatus.PENDING:
            raise EmailSendExecutionError(
                f"action {action.id} is pending; review and approve it before execution"
            )
        if action.status in {ActionStatus.EXECUTING, ActionStatus.FAILED}:
            if resume:
                return
            raise EmailSendExecutionError(
                f"action {action.id} is {action.status.value}; use --resume to retry safely"
            )
        if action.status is not ActionStatus.APPROVED:
            raise EmailSendExecutionError(
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
