"""Durable run ledger shared by independent agents."""

from __future__ import annotations

from typing import Any

from careeros.clock import Clock
from careeros.database.enums import (
    AgentRunStatus,
    ConnectorRunStatus,
)
from careeros.database.models import AgentRun, ConnectorRun
from careeros.database.session import Database
from careeros.services.audit import AuditService


class RunLedger:
    def __init__(self, database: Database, clock: Clock, audit: AuditService) -> None:
        self._database = database
        self._clock = clock
        self._audit = audit

    def start_agent(
        self,
        *,
        name: str,
        version: str,
        prompt_name: str | None,
        prompt_sha256: str | None,
        input_json: dict[str, Any],
        model: str | None = None,
    ) -> str:
        with self._database.session() as session:
            run = AgentRun(
                agent_name=name,
                agent_version=version,
                prompt_name=prompt_name,
                prompt_sha256=prompt_sha256,
                model=model,
                status=AgentRunStatus.RUNNING,
                input_json=input_json,
                started_at=self._clock.now(),
            )
            session.add(run)
            session.flush()
            return run.id

    def finish_agent(
        self,
        run_id: str,
        *,
        output_json: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        with self._database.session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError(f"agent run disappeared: {run_id}")
            run.finished_at = self._clock.now()
            run.status = AgentRunStatus.FAILED if error is not None else AgentRunStatus.SUCCEEDED
            run.output_json = output_json
            run.error = str(error) if error is not None else None
            self._audit.append(
                session,
                actor=run.agent_name,
                event_type=("agent.run_failed" if error is not None else "agent.run_succeeded"),
                aggregate_type="agent_run",
                aggregate_id=run.id,
                payload={
                    "agent_version": run.agent_version,
                    "prompt_sha256": run.prompt_sha256,
                    "error": str(error) if error is not None else None,
                },
            )

    def start_connector(
        self,
        *,
        name: str,
        operation: str,
        request_json: dict[str, Any],
    ) -> str:
        with self._database.session() as session:
            run = ConnectorRun(
                connector_name=name,
                operation=operation,
                status=ConnectorRunStatus.RUNNING,
                request_json=request_json,
                started_at=self._clock.now(),
            )
            session.add(run)
            session.flush()
            return run.id

    def finish_connector(
        self,
        run_id: str,
        *,
        response_summary_json: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        with self._database.session() as session:
            run = session.get(ConnectorRun, run_id)
            if run is None:
                raise RuntimeError(f"connector run disappeared: {run_id}")
            run.finished_at = self._clock.now()
            run.status = (
                ConnectorRunStatus.FAILED if error is not None else ConnectorRunStatus.SUCCEEDED
            )
            run.response_summary_json = response_summary_json
            run.error = str(error) if error is not None else None
            self._audit.append(
                session,
                actor=run.connector_name,
                event_type=(
                    "connector.run_failed" if error is not None else "connector.run_succeeded"
                ),
                aggregate_type="connector_run",
                aggregate_id=run.id,
                payload={
                    "operation": run.operation,
                    "error": str(error) if error is not None else None,
                },
            )
