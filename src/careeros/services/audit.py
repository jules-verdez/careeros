"""Tamper-evident append-only audit trail."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.base import new_id
from careeros.database.models import AuditEvent
from careeros.services.normalization import hash_payload, serialize_instant


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    events_checked: int
    first_invalid_sequence: int | None = None


class AuditService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def append(
        self,
        session: Session,
        *,
        actor: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
        action_request_id: str | None = None,
    ) -> AuditEvent:
        previous = session.scalar(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1))
        event_id = new_id()
        occurred_at = self._clock.now()
        previous_hash = previous.event_hash if previous is not None else None
        payload_json = payload or {}
        event_hash = self._calculate_hash(
            event_id=event_id,
            occurred_at=occurred_at,
            actor=actor,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            action_request_id=action_request_id,
            payload=payload_json,
            previous_hash=previous_hash,
        )
        event = AuditEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            actor=actor,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            action_request_id=action_request_id,
            payload_json=payload_json,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )
        session.add(event)
        session.flush()
        return event

    def verify(self, session: Session) -> AuditVerification:
        events = session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)).all()
        previous_hash: str | None = None
        for event in events:
            expected = self._calculate_hash(
                event_id=event.event_id,
                occurred_at=event.occurred_at,
                actor=event.actor,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                action_request_id=event.action_request_id,
                payload=event.payload_json,
                previous_hash=previous_hash,
            )
            if event.previous_hash != previous_hash or event.event_hash != expected:
                return AuditVerification(
                    valid=False,
                    events_checked=event.sequence - 1,
                    first_invalid_sequence=event.sequence,
                )
            previous_hash = event.event_hash
        return AuditVerification(valid=True, events_checked=len(events))

    @staticmethod
    def _calculate_hash(
        *,
        event_id: str,
        occurred_at: Any,
        actor: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        action_request_id: str | None,
        payload: dict[str, Any],
        previous_hash: str | None,
    ) -> str:
        return hash_payload(
            {
                "event_id": event_id,
                "occurred_at": serialize_instant(occurred_at),
                "actor": actor,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "action_request_id": action_request_id,
                "payload": payload,
                "previous_hash": previous_hash,
            }
        )
