"""Outreach limits and reply threading: short bodies, plain links, threaded follow-ups."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.enums import ActionKind, DocumentKind, EmailStatus
from careeros.database.models import ActionRequest, Company, Document, Email
from careeros.domain.records import MAX_OUTREACH_WORDS, DraftMessage
from careeros.services.email_drafts import EmailDraftExecutor
from careeros.services.normalization import normalize_text, sha256_text
from careeros.services.outreach import OutreachService


class RecordingMailConnector:
    name = "google-workspace-gmail"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "idempotency_key": idempotency_key,
                "in_reply_to": in_reply_to,
                "thread_id": thread_id,
            }
        )
        return f"draft-{len(self.calls)}"


def _message(body: str) -> DraftMessage:
    paragraphs = [part for part in body.split("\n\n") if part.strip()]
    return DraftMessage(
        subject="Follow-up",
        body=body,
        paragraph_rationale=tuple("reason" for _ in paragraphs),
        evidence_document_ids=(),
    )


def test_outreach_body_over_the_word_limit_is_rejected() -> None:
    body = " ".join(["word"] * (MAX_OUTREACH_WORDS + 1))
    with pytest.raises(ValidationError, match="limit is"):
        _message(body)
    _message(" ".join(["word"] * MAX_OUTREACH_WORDS))


@pytest.mark.parametrize(
    "link",
    [
        "https://www.google.com/url?q=https://github.com/x/y&source=gmail",
        "https://eur01.safelinks.protection.outlook.com/?url=https://x.test",
        "https://x.test/page?utm_source=newsletter",
    ],
)
def test_outreach_body_with_redirect_or_tracking_link_is_rejected(link: str) -> None:
    with pytest.raises(ValidationError, match="tracking link"):
        _message(f"See {link}")
    _message("See https://example.test/projects/demo")


def _person_with_public_email(container: Container) -> str:
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
            linkedin_url="https://www.linkedin.com/in/contact-example",
            email="recipient@example.test",
            email_source_url="https://example.test/team/contact",
            actor="test",
        )
        return person.id


def test_follow_up_replies_inside_the_sent_thread(
    container: Container, active_profile: str
) -> None:
    person_id = _person_with_public_email(container)
    outreach = OutreachService(container.approvals, container.audit)
    with container.database.session() as session:
        first, _ = outreach.stage_email(
            session,
            profile_id=active_profile,
            person_id=person_id,
            message=_message("First note."),
            connector_name="gmail",
            actor="test",
        )
        first_id = first.id

    with container.database.session() as session:
        with pytest.raises(ValueError, match="actually sent"):
            outreach.stage_email(
                session,
                profile_id=active_profile,
                person_id=person_id,
                message=_message("Too early."),
                connector_name="gmail",
                actor="test",
                in_reply_to_email_id=first_id,
            )

    with container.database.session() as session:
        parent = session.get(Email, first_id)
        assert parent is not None
        parent.status = EmailStatus.SENT
        parent.rfc822_message_id = "<parent@careeros.local>"
        parent.external_thread_id = "thread-42"
        parent.external_message_id = "msg-42"

    with container.database.session() as session:
        reply, action_id = outreach.stage_email(
            session,
            profile_id=active_profile,
            person_id=person_id,
            message=_message("A short follow-up."),
            connector_name="gmail",
            actor="test",
            in_reply_to_email_id=first_id,
        )
        reply_id = reply.id
        action = session.get(ActionRequest, action_id)
        assert action is not None
        assert action.kind is ActionKind.CREATE_EMAIL_DRAFT
        assert action.payload_json["in_reply_to_rfc822_message_id"] == "<parent@careeros.local>"
        assert action.payload_json["external_thread_id"] == "thread-42"
        container.approvals.approve(session, action_id, reviewer="Demo Candidate")

    fake = RecordingMailConnector()
    registry = ConnectorRegistry()
    registry.register("mail", fake)
    executor = EmailDraftExecutor(container.database, registry, container.clock, container.audit)
    executor.execute(action_id, actor="Demo Candidate")

    assert fake.calls[0]["in_reply_to"] == "<parent@careeros.local>"
    assert fake.calls[0]["thread_id"] == "thread-42"
    with container.database.session() as session:
        saved = session.get(Email, reply_id)
        assert saved is not None
        assert saved.in_reply_to_email_id == first_id
        assert saved.external_thread_id == "thread-42"
        assert saved.external_message_id == "draft-1"


def test_ordinary_draft_payload_has_no_thread_keys(
    container: Container, active_profile: str
) -> None:
    person_id = _person_with_public_email(container)
    with container.database.session() as session:
        evidence = Document(
            kind=DocumentKind.JOB_DESCRIPTION,
            title="Posting",
            media_type="text/plain",
            content_text="A posting.",
            sha256=sha256_text("A posting."),
        )
        session.add(evidence)
        session.flush()
        _, action_id = OutreachService(container.approvals, container.audit).stage_email(
            session,
            profile_id=active_profile,
            person_id=person_id,
            message=DraftMessage(
                subject="Hello",
                body="One paragraph.",
                paragraph_rationale=("reason",),
                evidence_document_ids=(evidence.id,),
            ),
            connector_name="gmail",
            actor="test",
        )
        action = session.get(ActionRequest, action_id)
        assert action is not None
        assert "external_thread_id" not in action.payload_json
        assert "in_reply_to_rfc822_message_id" not in action.payload_json
