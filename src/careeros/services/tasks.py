"""Operator updates to the deterministic work queue."""

from __future__ import annotations

from sqlalchemy.orm import Session

from careeros.database.enums import TaskStatus
from careeros.database.models import Task
from careeros.services.audit import AuditService


class TaskService:
    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    def complete(self, session: Session, task_id: str, *, actor: str) -> Task:
        task = self._require(session, task_id)
        task.status = TaskStatus.DONE
        self._audit.append(
            session,
            actor=actor,
            event_type="task.completed",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={},
        )
        return task

    def reopen(self, session: Session, task_id: str, *, actor: str) -> Task:
        """Undo a completion. Cancelled tasks were retired by policy and stay retired."""
        task = self._require(session, task_id)
        if task.status is not TaskStatus.DONE:
            raise ValueError(f"only completed tasks can be reopened; this one is {task.status}")
        task.status = TaskStatus.OPEN
        self._audit.append(
            session,
            actor=actor,
            event_type="task.reopened",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={},
        )
        return task

    @staticmethod
    def _require(session: Session, task_id: str) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        return task
