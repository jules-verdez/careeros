from __future__ import annotations

import pytest
from sqlalchemy import select, update

from careeros.container import Container
from careeros.database.enums import ActionKind, ActionStatus
from careeros.database.models import AuditEvent
from careeros.services.approvals import InvalidActionTransitionError
from careeros.services.normalization import hash_payload


def test_approval_is_bound_to_payload_and_cannot_be_reviewed_twice(
    container: Container,
    active_profile: str,
) -> None:
    del active_profile
    payload = {"email_id": "email-123"}
    with container.database.session() as session:
        action = container.approvals.request(
            session,
            kind=ActionKind.SEND_EMAIL,
            summary="Send reviewed email",
            connector_name="gmail",
            payload=payload,
            idempotency_key="send-email:email-123",
            requested_by="email-agent",
        )
        action_id = action.id
        assert action.payload_sha256 == hash_payload(payload)
    with container.database.session() as session:
        approved = container.approvals.approve(session, action_id, reviewer="Demo Candidate")
        assert approved.status is ActionStatus.APPROVED
    with container.database.session() as session:
        with pytest.raises(InvalidActionTransitionError):
            container.approvals.reject(
                session,
                action_id,
                reviewer="Demo Candidate",
                reason="changed mind",
            )


def test_audit_chain_detects_tampering(
    container: Container,
    active_profile: str,
) -> None:
    del active_profile
    with container.database.session() as session:
        container.audit.append(
            session,
            actor="test",
            event_type="test.event",
            aggregate_type="test",
            aggregate_id="1",
            payload={"value": "original"},
        )
    with container.database.session() as session:
        assert container.audit.verify(session).valid
        first = session.scalar(select(AuditEvent).order_by(AuditEvent.sequence))
        assert first is not None
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.sequence == first.sequence)
            .values(payload_json={"value": "tampered"})
        )
    with container.database.session() as session:
        result = container.audit.verify(session)
        assert not result.valid
        assert result.first_invalid_sequence is not None
