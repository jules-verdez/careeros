"""Vibe Prospecting (Explorium) connector: people and company data, billed in credits.

Explorium's AgentSource API powers Vibe Prospecting. Searching prospects and matching
companies returns professional profiles; contact details cost extra credits per person
(2 for an email, 5 for a phone). Addresses returned here are vendor observations, not
public sources, so CareerOS never promotes them into the sendable-contact path.

Built on ``urllib`` like the Hunter connector, so it adds no HTTP dependency.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from careeros.connectors.base import ConnectorNotConfiguredError

API_ROOT = "https://api.explorium.ai/v1"
#: Credits Explorium charges per contact enrichment, by requested contact type.
CONTACT_CREDITS = {"email": 2, "phone": 5}
PROSPECT_FIELDS = (
    "prospect_id",
    "full_name",
    "job_title",
    "job_level_main",
    "job_department_main",
    "company_name",
    "company_website",
    "linkedin",
    "country_name",
    "city",
    "business_id",
)


def contact_credits(contact_types: tuple[str, ...]) -> int:
    """Explorium bills a combined email+phone lookup at the phone price."""
    return max((CONTACT_CREDITS[kind] for kind in contact_types), default=0)


class ExploriumConnector:
    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("EXPLORIUM_API_KEY")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "vibe-prospecting"

    def healthcheck(self) -> None:
        if not self._api_key:
            raise ConnectorNotConfiguredError(
                "EXPLORIUM_API_KEY is not set; add your Vibe Prospecting API key."
            )

    def credits(self) -> dict[str, Any]:
        """Remaining and allocated credits. Free to call."""
        body = self._request("GET", "credits")
        return {
            "remaining_credits": body.get("remaining_credits"),
            "allocated_credits": body.get("allocated_credits"),
        }

    def match_business(self, *, name: str | None, domain: str | None) -> str | None:
        """Explorium's 32-character business id for a company, or None when unmatched."""
        candidate = {key: value for key, value in (("name", name), ("domain", domain)) if value}
        if not candidate:
            raise ValueError("Give a company name, a domain, or both.")
        body = self._request("POST", "businesses/match", {"businesses_to_match": [candidate]})
        matches = body.get("matched_businesses") or []
        first = matches[0] if matches and isinstance(matches[0], dict) else {}
        business_id = first.get("business_id")
        return str(business_id) if business_id else None

    def fetch_prospects(
        self,
        *,
        business_ids: tuple[str, ...] = (),
        company_names: tuple[str, ...] = (),
        job_titles: tuple[str, ...] = (),
        job_levels: tuple[str, ...] = (),
        departments: tuple[str, ...] = (),
        country_codes: tuple[str, ...] = (),
        require_email: bool = False,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Professional profiles matching every given filter; values within one filter OR."""
        filters: dict[str, Any] = {}
        if business_ids:
            filters["business_id"] = {"values": list(business_ids)}
        if company_names:
            filters["company_name"] = {"values": list(company_names)}
        if job_titles:
            filters["job_title"] = {"values": list(job_titles), "include_related_job_titles": True}
        if job_levels:
            filters["job_level"] = {"values": list(job_levels)}
        if departments:
            filters["job_department"] = {"values": list(departments)}
        if country_codes:
            filters["country_code"] = {"values": [code.upper() for code in country_codes]}
        if require_email:
            filters["has_contact_details"] = {"value": "email"}
        if not filters:
            raise ValueError("Give at least one filter, such as a company or a job title.")
        size = max(1, min(limit, 100))
        body = self._request(
            "POST",
            "prospects",
            {"mode": "full", "size": size, "page_size": size, "page": 1, "filters": filters},
        )
        rows = body.get("data") or []
        return [
            {field: row.get(field) for field in PROSPECT_FIELDS}
            for row in rows[:size]
            if isinstance(row, dict)
        ]

    def contact_information(
        self, prospect_id: str, *, contact_types: tuple[str, ...] = ("email",)
    ) -> dict[str, Any]:
        """Paid lookup of one prospect's contact details."""
        if not contact_types or any(kind not in CONTACT_CREDITS for kind in contact_types):
            raise ValueError("contact_types must contain 'email', 'phone', or both.")
        body = self._request(
            "POST",
            "prospects/contacts_information/enrich",
            {"prospect_id": prospect_id, "parameters": {"contact_types": list(contact_types)}},
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        assert isinstance(data, dict)
        result: dict[str, Any] = {
            "professional_email": data.get("professions_email"),
            "professional_email_status": data.get("professional_email_status"),
            "emails": data.get("emails") or [],
        }
        if "phone" in contact_types:
            result["mobile_phone"] = data.get("mobile_phone")
            result["phone_numbers"] = data.get("phone_numbers") or []
        return result

    def _request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        self.healthcheck()
        assert self._api_key is not None
        request = Request(
            f"{API_ROOT}/{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "api_key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code in {401, 403}:
                raise ConnectorNotConfiguredError(
                    f"Vibe Prospecting rejected the API key ({error.code})."
                ) from error
            raise RuntimeError(
                f"Vibe Prospecting request failed ({error.code}): {detail}"
            ) from error
        except URLError as error:
            raise RuntimeError(f"Vibe Prospecting is unreachable: {error.reason}") from error
        return body if isinstance(body, dict) else {}
