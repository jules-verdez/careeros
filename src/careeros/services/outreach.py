"""Evidence-bound outreach drafting and approval staging."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.enums import (
    ActionKind,
    ContactDeliveryStatus,
    Direction,
    DocumentKind,
    EmailStatus,
    RecipientKind,
)
from careeros.database.models import (
    CandidateProfile,
    Document,
    Email,
    EmailEvidence,
    EmailParagraphRationale,
    EmailRecipient,
    Person,
    PersonContact,
)
from careeros.domain.records import DraftMessage
from careeros.services.approvals import ApprovalService
from careeros.services.audit import AuditService
from careeros.services.contact_policy import ContactPolicy
from careeros.services.email_drafts import load_email_draft_packet
from careeros.services.normalization import sha256_text


class OutreachService:
    def __init__(
        self,
        approvals: ApprovalService,
        audit: AuditService,
        contact_policy: ContactPolicy | None = None,
    ) -> None:
        self._approvals = approvals
        self._audit = audit
        self._contact_policy = contact_policy or ContactPolicy()

    def stage_email(
        self,
        session: Session,
        *,
        profile_id: str,
        person_id: str,
        message: DraftMessage,
        connector_name: str,
        actor: str,
        application_id: str | None = None,
        in_reply_to_email_id: str | None = None,
    ) -> tuple[Email, str]:
        profile = session.get(CandidateProfile, profile_id)
        if profile is None:
            raise LookupError(f"candidate profile not found: {profile_id}")
        if not profile.primary_email:
            raise ValueError("candidate profile needs a primary email before outreach")
        person = session.get(Person, person_id)
        if person is None:
            raise LookupError(f"person not found: {person_id}")
        self._contact_policy.check_person(session, person_id)
        contact = session.scalar(
            select(PersonContact)
            .where(
                PersonContact.person_id == person_id,
                PersonContact.contact_type == "email",
                PersonContact.is_public.is_(True),
                PersonContact.delivery_status != ContactDeliveryStatus.INVALID,
            )
            .order_by(PersonContact.verified_at.desc())
            .limit(1)
        )
        if contact is None or not contact.public_source_url:
            msg = "recipient has no publicly sourced email address"
            raise ValueError(msg)
        parent: Email | None = None
        if in_reply_to_email_id is not None:
            parent = session.get(Email, in_reply_to_email_id)
            if parent is None:
                raise LookupError(f"parent email not found: {in_reply_to_email_id}")
            if parent.direction is not Direction.OUTBOUND or parent.status is not EmailStatus.SENT:
                msg = "a follow-up can only reply to an outbound email that was actually sent"
                raise ValueError(msg)
            if parent.person_id != person.id:
                msg = "a follow-up must reply to a message sent to the same person"
                raise ValueError(msg)
            if application_id is None:
                application_id = parent.application_id
        evidence = []
        for document_id in message.evidence_document_ids:
            document = session.get(Document, document_id)
            if document is None:
                raise LookupError(f"evidence document not found: {document_id}")
            evidence.append(document)
        body_digest = sha256_text(message.body)
        body_document = session.scalar(select(Document).where(Document.sha256 == body_digest))
        if body_document is None:
            body_document = Document(
                kind=DocumentKind.EMAIL,
                title=message.subject,
                media_type="text/plain",
                content_text=message.body,
                sha256=body_digest,
            )
            session.add(body_document)
            session.flush()
        email = Email(
            application_id=application_id,
            person_id=person.id,
            connector_name=connector_name,
            direction=Direction.OUTBOUND,
            status=EmailStatus.DRAFT,
            sender_address=profile.primary_email,
            subject=message.subject,
            body_document_id=body_document.id,
            in_reply_to_email_id=parent.id if parent else None,
        )
        session.add(email)
        session.flush()
        session.add(
            EmailRecipient(
                email_id=email.id,
                kind=RecipientKind.TO,
                address=contact.normalized_value,
                person_id=person.id,
            )
        )
        for index, rationale in enumerate(message.paragraph_rationale):
            session.add(
                EmailParagraphRationale(
                    email_id=email.id,
                    paragraph_index=index,
                    rationale=rationale,
                )
            )
        for document in evidence:
            session.add(EmailEvidence(email_id=email.id, document_id=document.id))
        session.flush()
        packet = load_email_draft_packet(session, email.id)
        action = self._approvals.request(
            session,
            kind=ActionKind.CREATE_EMAIL_DRAFT,
            summary=f"Create Gmail draft to {person.full_name}: {message.subject}",
            connector_name=connector_name,
            payload=packet.approval_payload,
            idempotency_key=f"create-email-draft:{email.id}:{body_digest}",
            requested_by=actor,
        )
        self._audit.append(
            session,
            actor=actor,
            event_type="email.draft_staged",
            aggregate_type="email",
            aggregate_id=email.id,
            action_request_id=action.id,
            payload={
                "person_id": person.id,
                "recipient_source_url": contact.public_source_url,
                "body_sha256": body_digest,
                "evidence_document_ids": list(message.evidence_document_ids),
                "in_reply_to_email_id": parent.id if parent else None,
            },
        )
        return email, action.id
