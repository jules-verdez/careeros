"""Continuous job-discovery orchestration."""

from __future__ import annotations

from careeros.agents.runs import RunLedger
from careeros.connectors.base import JobDiscoveryConnector
from careeros.database.session import Database
from careeros.domain.records import JobDiscoveryQuery
from careeros.prompts import PromptLibrary
from careeros.services.job_ingestion import IngestionSummary, JobIngestionService


class DiscoveryAgent:
    NAME = "job-discovery-agent"
    VERSION = "1.0.0"
    PROMPT = "P02_JOB_DISCOVERY.md"

    def __init__(
        self,
        database: Database,
        ledger: RunLedger,
        prompts: PromptLibrary,
        ingestion: JobIngestionService,
    ) -> None:
        self._database = database
        self._ledger = ledger
        self._prompts = prompts
        self._ingestion = ingestion

    def run(
        self,
        connector: JobDiscoveryConnector,
        query: JobDiscoveryQuery,
    ) -> IngestionSummary:
        prompt = self._prompts.load(self.PROMPT)
        input_json = query.model_dump(mode="json")
        agent_run_id = self._ledger.start_agent(
            name=self.NAME,
            version=self.VERSION,
            prompt_name=prompt.name,
            prompt_sha256=prompt.sha256,
            input_json=input_json,
        )
        connector_run_id = self._ledger.start_connector(
            name=connector.name,
            operation="discover_jobs",
            request_json=input_json,
        )
        try:
            connector.healthcheck()
            records = list(connector.discover(query))
        except Exception as error:
            self._ledger.finish_connector(connector_run_id, error=error)
            self._ledger.finish_agent(agent_run_id, error=error)
            raise
        self._ledger.finish_connector(
            connector_run_id,
            response_summary_json={"records": len(records)},
        )
        try:
            with self._database.session() as session:
                summary = self._ingestion.ingest(
                    session,
                    records,
                    actor=self.NAME,
                    connector_name=connector.name,
                )
        except Exception as error:
            self._ledger.finish_agent(agent_run_id, error=error)
            raise
        self._ledger.finish_agent(agent_run_id, output_json=summary.as_dict())
        return summary
