from __future__ import annotations

import pytest
from pydantic import ValidationError

from careeros.container import Container
from careeros.database.enums import ActionKind, ActionStatus, DocumentKind, EmailStatus
from careeros.database.models import ActionRequest, Company, Document, Email
from careeros.domain.records import DraftMessage
from careeros.services.normalization import normalize_text, sha256_text
from careeros.services.outreach import OutreachService


def test_public_email_needs_source_and_outreach_never_sends(
    container: Container,
    active_profile: str,
) -> None:
    with container.database.session() as session:
        company = Company(
            canonical_name="Example Capital",
            normalized_name=normalize_text("Example Capital"),
            domain="example.test",
        )
        evidence = Document(
            kind=DocumentKind.COMPANY_RESEARCH_MARKDOWN,
            title="Research",
            media_type="text/markdown",
            content_text="Verified company evidence.",
            sha256=sha256_text("Verified company evidence."),
        )
        session.add_all([company, evidence])
        session.flush()
        company_id = company.id
        evidence_id = evidence.id

    with container.database.session() as session:
        with pytest.raises(ValueError, match="email-source-url"):
            container.people.add_public_person(
                session,
                company_id=company_id,
                full_name="Sample Contact",
                title="Head of Quant",
                influence_probability=0.9,
                confidence=0.8,
                linkedin_url=None,
                email="recipient@example.test",
                email_source_url=None,
                actor="test",
            )

    with container.database.session() as session:
        person = container.people.add_public_person(
            session,
            company_id=company_id,
            full_name="Sample Contact",
            title="Head of Quant",
            influence_probability=0.9,
            confidence=0.8,
            linkedin_url="https://www.linkedin.com/in/contact-example",
            email="recipient@example.test",
            email_source_url="https://example.test/team/contact",
            actor="test",
        )
        person_id = person.id

    message = DraftMessage(
        subject="Relevant research",
        body=(
            "This message refers to the supplied example document.\n\n"
            "This is a fictional follow-up request."
        ),
        paragraph_rationale=(
            "Establishes cited relevance.",
            "Makes one low-friction request.",
        ),
        evidence_document_ids=(evidence_id,),
    )
    outreach = OutreachService(container.approvals, container.audit)
    with container.database.session() as session:
        email, action_id = outreach.stage_email(
            session,
            profile_id=active_profile,
            person_id=person_id,
            message=message,
            connector_name="gmail",
            actor="email-generator-agent",
        )
        email_id = email.id
    with container.database.session() as session:
        saved_email = session.get(Email, email_id)
        action = session.get(ActionRequest, action_id)
        assert saved_email is not None
        assert saved_email.status is EmailStatus.DRAFT
        assert saved_email.external_message_id is None
        assert action is not None
        assert action.status is ActionStatus.PENDING
        assert action.kind is ActionKind.CREATE_EMAIL_DRAFT
        assert action.payload_json["operation"] == "create_email_draft"
        assert action.payload_json["email_id"] == email_id
        assert action.payload_json["recipient"] == "recipient@example.test"
        assert action.payload_json["body_sha256"] == sha256_text(message.body)


def test_draft_requires_one_rationale_per_paragraph() -> None:
    with pytest.raises(ValidationError, match="exactly one explanation"):
        DraftMessage(
            subject="Subject",
            body="First.\n\nSecond.",
            paragraph_rationale=("Only one.",),
            evidence_document_ids=(),
        )
