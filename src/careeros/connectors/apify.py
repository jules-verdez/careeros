"""Apify connector implementations."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient

from careeros.connectors.base import (
    ConnectorNotConfiguredError,
    JobDiscoveryConnector,
    PeopleDiscoveryConnector,
)
from careeros.database.enums import SourceKind
from careeros.domain.records import JobDiscoveryQuery, JobRecord, PersonCandidate

# Runs at import rather than before it: every token here is read in __init__, so this
# only has to precede construction. Sitting above the imports bought nothing and broke
# import ordering, which is the same reasoning the Hunter connector documents.


def _nested(value: Any, key: str) -> Any:
    """Read ``key`` from a value that some boards send as an object and others as a string."""
    if isinstance(value, dict):
        return value.get(key)
    return value


def dataset_id_of(run: Any) -> str:
    """Return the dataset a finished run wrote to, or refuse to read a run that failed.

    ``call`` returns None when the run could not be started, and a ``Run`` model
    otherwise, so every caller here needs the same two checks before it can page a
    dataset. Reading a run that is not SUCCEEDED is the documented way to end up with a
    partial dataset that looks complete.
    """
    if run is None:
        raise RuntimeError("Apify actor did not start; no run was returned.")
    payload = run if isinstance(run, dict) else run.model_dump(by_alias=True)
    if payload.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Apify actor failed: status={payload.get('status')!r}")
    dataset_id = payload.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify run succeeded but reported no dataset.")
    return str(dataset_id)


class ApifyLinkedInProfileConnector(PeopleDiscoveryConnector):
    """Discovers decision-makers via Apify LinkedIn scrapers."""

    def __init__(self, actor_id: str = "curious_coder/linkedin-profile-scraper") -> None:
        self.actor_id = actor_id
        self._token = os.environ.get("APIFY_TOKEN")

    @property
    def name(self) -> str:
        return "apify-linkedin-profiles"

    def healthcheck(self) -> None:
        if not self._token:
            msg = "APIFY_TOKEN environment variable is not set."
            raise ConnectorNotConfiguredError(msg)
        try:
            client = ApifyClient(self._token)
            client.user().get()
        except Exception as error:
            msg = f"Failed to authenticate with Apify: {error}"
            raise ConnectorNotConfiguredError(msg) from error

    def discover_people(self, company_name: str) -> Iterable[PersonCandidate]:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN not set.")

        client = ApifyClient(self._token)

        run_input = {
            "searchUrl": (
                "https://www.linkedin.com/search/results/people/?keywords="
                f"{company_name} (CTO OR Head OR Quant OR Research OR Trading)"
            ),
            "maxProfiles": 10,
        }

        run = client.actor(self.actor_id).call(run_input=run_input)
        dataset = client.dataset(dataset_id_of(run))

        for item in dataset.iterate_items():
            full_name = item.get("fullName", "")
            if not full_name:
                continue

            yield PersonCandidate(
                full_name=full_name,
                role=item.get("headline", "Unknown Role")[:255],
                location=item.get("location", None),
                linkedin_url=item.get("url", None),
                github_url=None,
                website_url=None,
                public_email=None,
                public_email_source_url=None,
                recent_publications=(),
                mutual_connections=(),
                influence_probability=0.8,
                confidence=0.9,
            )


class ApifyGreenhouseConnector(JobDiscoveryConnector):
    """Discovers jobs via Apify Greenhouse scraper."""

    def __init__(self, actor_id: str = "fantastic-jobs/greenhouse-jobs-api") -> None:
        self.actor_id = actor_id
        self._token = os.environ.get("APIFY_TOKEN")

    @property
    def name(self) -> str:
        return "apify-greenhouse"

    def healthcheck(self) -> None:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN environment variable is not set.")

    def discover(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN not set.")

        client = ApifyClient(self._token)

        run_input = {
            "searchQuery": " ".join(query.roles) if query.roles else "Software",
            "limit": max(200, query.limit),
        }

        run = client.actor(self.actor_id).call(run_input=run_input)
        dataset = client.dataset(dataset_id_of(run))

        for item in dataset.iterate_items():
            yield JobRecord(
                source_kind=SourceKind.CAREER_PAGE,
                source_name="Greenhouse",
                external_id=str(item.get("id")),
                source_url=item.get("absolute_url")
                or item.get("url", "https://boards.greenhouse.io"),
                company_name=item.get("company", "Unknown"),
                title=item.get("title", "Unknown"),
                location=_nested(item.get("location"), "name"),
                posted_at=None,
                raw=item,
            )


class ApifyLeverConnector(JobDiscoveryConnector):
    """Discovers jobs via Apify Lever scraper."""

    def __init__(self, actor_id: str = "fantastic-jobs/lever-jobs-api") -> None:
        self.actor_id = actor_id
        self._token = os.environ.get("APIFY_TOKEN")

    @property
    def name(self) -> str:
        return "apify-lever"

    def healthcheck(self) -> None:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN environment variable is not set.")

    def discover(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN not set.")

        client = ApifyClient(self._token)

        run_input = {
            "searchQuery": " ".join(query.roles) if query.roles else "Software",
            "limit": query.limit,
        }

        run = client.actor(self.actor_id).call(run_input=run_input)
        dataset = client.dataset(dataset_id_of(run))

        for item in dataset.iterate_items():
            yield JobRecord(
                source_kind=SourceKind.LEVER,
                source_name="Lever",
                external_id=str(item.get("id")),
                source_url=item.get("hostedUrl") or item.get("url", "https://jobs.lever.co"),
                company_name=item.get("company", "Unknown"),
                title=item.get("text", "Unknown"),
                location=_nested(item.get("categories"), "location") or item.get("location"),
                posted_at=None,
                raw=item,
            )


class ApifyGlassdoorConnector(JobDiscoveryConnector):
    """Discovers jobs via Apify Glassdoor scraper."""

    def __init__(self, actor_id: str = "bebity/glassdoor-jobs-scraper") -> None:
        self.actor_id = actor_id
        self._token = os.environ.get("APIFY_TOKEN")

    @property
    def name(self) -> str:
        return "apify-glassdoor"

    def healthcheck(self) -> None:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN environment variable is not set.")

    def discover(self, query: JobDiscoveryQuery) -> Iterable[JobRecord]:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN not set.")

        client = ApifyClient(self._token)

        run_input = {
            "keyword": " ".join(query.roles) if query.roles else "Quant",
            "limit": query.limit,
        }

        run = client.actor(self.actor_id).call(run_input=run_input)
        dataset = client.dataset(dataset_id_of(run))

        for item in dataset.iterate_items():
            yield JobRecord(
                source_kind=SourceKind.OTHER,
                source_name="Glassdoor",
                external_id=str(item.get("id", item.get("jobId"))),
                source_url=item.get("url", "https://glassdoor.com"),
                company_name=item.get("employerName", "Unknown"),
                title=item.get("jobTitleText", "Unknown"),
                location=item.get("locationName", None),
                posted_at=None,
                raw=item,
            )


class ApifyBudgetExceededError(RuntimeError):
    """A run's spending cap would not fit in what is left of the monthly limit."""


class ApifyActorRunner:
    """Runs any Apify Store actor under an explicit, budget-checked spending cap.

    Apify silently lowers a run's cap to whatever remains of the monthly limit, and runs
    started together each receive that same remainder, so concurrent runs overshoot.
    This runner reads the account limits first and refuses a cap that does not fit,
    and it always passes ``max_total_charge_usd`` so a single run cannot exceed it.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("APIFY_TOKEN")

    @property
    def name(self) -> str:
        return "apify-actors"

    def healthcheck(self) -> None:
        if not self._token:
            raise ConnectorNotConfiguredError("APIFY_TOKEN environment variable is not set.")

    def _client(self) -> ApifyClient:
        self.healthcheck()
        return ApifyClient(self._token)

    def budget(self) -> dict[str, Any]:
        """Monthly limit, usage, and what remains. Free to call."""
        limits = self._client().user().limits()
        cap = limits.limits.max_monthly_usage_usd if limits.limits else None
        used = limits.current.monthly_usage_usd if limits.current else 0.0
        cycle = limits.monthly_usage_cycle
        return {
            "monthly_limit_usd": cap,
            "used_usd": round(used, 2),
            "remaining_usd": round(cap - used, 2) if cap is not None else None,
            "cycle_ends_at": cycle.end_at.isoformat() if cycle and cycle.end_at else None,
        }

    def search_store(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Find actors in the public Apify Store. Free to call."""
        page = self._client().store().list(search=query, limit=max(1, min(limit, 20)))
        actors = []
        for item in page.items:
            pricing = item.current_pricing_info
            actors.append(
                {
                    "actor_id": f"{item.username}/{item.name}",
                    "title": item.title,
                    "description": (item.description or "")[:300],
                    "pricing_model": getattr(pricing, "pricing_model", None),
                    "url": item.url,
                }
            )
        return actors

    def run(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_charge_usd: float,
        max_items: int,
        timeout_secs: int = 600,
    ) -> dict[str, Any]:
        """Run one actor to completion within ``max_charge_usd`` and return its items."""
        if max_charge_usd <= 0:
            raise ValueError("max_charge_usd must be positive.")
        budget = self.budget()
        remaining = budget["remaining_usd"]
        if remaining is not None and max_charge_usd > remaining:
            raise ApifyBudgetExceededError(
                f"This run may cost up to ${max_charge_usd:.2f} but only ${remaining:.2f} "
                f"remains of the ${budget['monthly_limit_usd']:.2f} monthly limit."
            )
        client = self._client()
        run = client.actor(actor_id).call(
            run_input=run_input,
            max_total_charge_usd=Decimal(str(max_charge_usd)),
            max_items=max_items,
            wait_duration=timedelta(seconds=timeout_secs),
        )
        dataset = client.dataset(dataset_id_of(run))
        items = dataset.list_items(limit=max_items, clean=True).items
        payload = run.model_dump(by_alias=True) if run is not None else {}
        return {
            "run_id": payload.get("id"),
            "status": payload.get("status"),
            "usage_usd": payload.get("usageTotalUsd"),
            "item_count": len(items),
            "items": items,
        }
