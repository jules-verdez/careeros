"""Daily CEO and morning review agents."""

from __future__ import annotations

from dataclasses import dataclass

from careeros.agents.runs import RunLedger
from careeros.database.models import CandidateProfile
from careeros.database.session import Database
from careeros.prompts import PromptLibrary
from careeros.services.daily import BriefView, DailyBriefService


@dataclass(frozen=True)
class DailyAgentResult:
    candidate_name: str
    view: BriefView


class DailyAgent:
    NAME = "daily-ceo-agent"
    VERSION = "1.0.0"
    PROMPT = "P09_DAILY_AGENT.md"

    def __init__(
        self,
        database: Database,
        ledger: RunLedger,
        prompts: PromptLibrary,
        briefs: DailyBriefService,
    ) -> None:
        self._database = database
        self._ledger = ledger
        self._prompts = prompts
        self._briefs = briefs

    def run(self, *, profile_id: str, limit: int = 3) -> DailyAgentResult:
        prompt = self._prompts.load(self.PROMPT)
        run_id = self._ledger.start_agent(
            name=self.NAME,
            version=self.VERSION,
            prompt_name=prompt.name,
            prompt_sha256=prompt.sha256,
            input_json={"profile_id": profile_id, "limit": limit},
        )
        try:
            with self._database.session() as session:
                profile = session.get(CandidateProfile, profile_id)
                if profile is None:
                    raise LookupError(f"candidate profile not found: {profile_id}")
                view = self._briefs.generate(
                    session,
                    profile=profile,
                    actor=self.NAME,
                    limit=limit,
                )
                result = DailyAgentResult(candidate_name=profile.name, view=view)
        except Exception as error:
            self._ledger.finish_agent(run_id, error=error)
            raise
        self._ledger.finish_agent(
            run_id,
            output_json={
                "daily_brief_id": result.view.brief.id,
                "revision": result.view.brief.revision,
                "recommendations": len(result.view.items),
                "mandatory_user_review": [
                    card.model_dump(mode="json")
                    for card in result.view.opportunity_queue.mandatory_user_review
                ],
            },
        )
        return result
