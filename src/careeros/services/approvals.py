"""Approval state machine for irreversible actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import ActionKind, ActionStatus
from careeros.database.models import ActionRequest
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload


class InvalidActionTransitionError(RuntimeError):
    pass


class ApprovalService:
    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self._clock = clock
        self._audit = audit
        self.validators: dict[ActionKind, Callable[[Session, ActionRequest], None]] = {}

    def request(
        self,
        session: Session,
        *,
        kind: ActionKind,
        summary: str,
        connector_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        requested_by: str,
    ) -> ActionRequest:
        action = ActionRequest(
            kind=kind,
            status=ActionStatus.PENDING,
            summary=summary,
            connector_name=connector_name,
            payload_json=payload,
            payload_sha256=hash_payload(payload),
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            requested_at=self._clock.now(),
        )
        session.add(action)
        session.flush()
        self._audit.append(
            session,
            actor=requested_by,
            event_type="action.requested",
            aggregate_type="action_request",
            aggregate_id=action.id,
            action_request_id=action.id,
            payload={
                "kind": kind.value,
                "connector_name": connector_name,
                "payload_sha256": action.payload_sha256,
            },
        )
        return action

    def approve(self, session: Session, action_id: str, *, reviewer: str) -> ActionRequest:
        action = self._require(session, action_id)
        if not reviewer.strip():
            raise ValueError("A named reviewer is required")
        if hash_payload(action.payload_json) != action.payload_sha256:
            raise ValueError("Action content changed after the approval request")
        if action.kind in self.validators:
            self.validators[action.kind](session, action)
        self._transition_from_pending(
            action,
            status=ActionStatus.APPROVED,
            reviewer=reviewer,
        )
        self._audit.append(
            session,
            actor=reviewer,
            event_type="action.approved",
            aggregate_type="action_request",
            aggregate_id=action.id,
            action_request_id=action.id,
            payload={"payload_sha256": action.payload_sha256},
        )
        return action

    def reject(
        self,
        session: Session,
        action_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> ActionRequest:
        action = self._require(session, action_id)
        self._transition_from_pending(
            action,
            status=ActionStatus.REJECTED,
            reviewer=reviewer,
        )
        action.error = reason
        self._audit.append(
            session,
            actor=reviewer,
            event_type="action.rejected",
            aggregate_type="action_request",
            aggregate_id=action.id,
            action_request_id=action.id,
            payload={"reason": reason},
        )
        return action

    def _transition_from_pending(
        self,
        action: ActionRequest,
        *,
        status: ActionStatus,
        reviewer: str,
    ) -> None:
        if action.status is not ActionStatus.PENDING:
            msg = f"cannot move action {action.id} from {action.status.value} to {status.value}"
            raise InvalidActionTransitionError(msg)
        action.status = status
        action.reviewed_by = reviewer
        action.reviewed_at = self._clock.now()

    @staticmethod
    def _require(session: Session, action_id: str) -> ActionRequest:
        action = session.get(ActionRequest, action_id)
        if action is None:
            msg = f"action request not found: {action_id}"
            raise LookupError(msg)
        return action
