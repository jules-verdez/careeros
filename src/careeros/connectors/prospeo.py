"""One-credit-at-a-time Prospeo fallback for Hunter misses and catch-all domains."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import HttpUrl

from careeros.connectors.base import ConnectorNotConfiguredError, ContactEnrichmentConnector
from careeros.domain.records import ContactEnrichmentResult

API_URL = "https://api.prospeo.io/enrich-person"
JsonRequester = Callable[[Request, float], dict[str, Any]]


class CreditSpendNotApprovedError(RuntimeError):
    """Raised before a provider request when the one-credit flag was omitted."""


class ProspeoConnector(ContactEnrichmentConnector):
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        requester: JsonRequester | None = None,
    ) -> None:
        self._key = os.environ.get("PROSPEO_API_KEY")
        self._timeout = timeout
        self._requester = requester or self._request_json

    @property
    def name(self) -> str:
        return "prospeo"

    def healthcheck(self) -> None:
        # Prospeo's account call is unnecessary here. Presence is checked without
        # consuming a lookup; authentication is authoritatively checked on enrichment.
        if not self._key:
            raise ConnectorNotConfiguredError("PROSPEO_API_KEY environment variable is not set.")

    def enrich_person(
        self,
        *,
        full_name: str,
        company_name: str,
        company_website: str,
        linkedin_url: str | None,
        allow_credit_spend: bool,
    ) -> ContactEnrichmentResult:
        if not allow_credit_spend:
            raise CreditSpendNotApprovedError(
                "Prospeo may spend one credit; re-run with --spend-one-credit."
            )
        self.healthcheck()
        if not company_website and not linkedin_url:
            raise ValueError("Prospeo requires a company website or a public LinkedIn URL.")
        data: dict[str, str] = {
            "full_name": full_name,
            "company_name": company_name,
            "company_website": company_website,
        }
        if linkedin_url:
            data["linkedin_url"] = linkedin_url
        body = {
            "only_verified_email": True,
            "enrich_mobile": False,
            "data": data,
        }
        request = Request(
            API_URL,
            method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "X-KEY": str(self._key),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "careeros-contact-enrichment",
            },
        )
        payload = self._requester(request, self._timeout)
        if payload.get("error"):
            detail = payload.get("message") or payload.get("error_message") or "unknown error"
            raise RuntimeError(f"Prospeo enrichment failed: {detail}")
        person = payload.get("person")
        person = person if isinstance(person, dict) else {}
        email = person.get("email")
        email = email if isinstance(email, dict) else {}
        status = str(email.get("status") or "not_found").casefold()
        address = email.get("email") if email.get("revealed") is True else None
        address = str(address).strip().casefold() if address else None
        verified = status == "verified" and bool(address)
        free_lookup = bool(payload.get("free_enrichment"))
        linkedin = person.get("linkedin_url")
        return ContactEnrichmentResult(
            provider=self.name,
            full_name=person.get("full_name") or full_name,
            email_address=address,
            email_status=status,
            is_verified=verified,
            current_job_title=person.get("current_job_title") or None,
            linkedin_url=linkedin or None,
            source_url=HttpUrl(API_URL),
            credits_spent=0 if free_lookup or not address else 1,
            free_lookup=free_lookup,
            response_sha256=self._response_sha256(payload),
        )

    @staticmethod
    def _response_sha256(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_json(request: Request, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in (401, 403):
                raise ConnectorNotConfiguredError("Prospeo rejected PROSPEO_API_KEY.") from error
            if error.code == 402:
                raise ConnectorNotConfiguredError("Prospeo credits are exhausted.") from error
            if error.code == 429:
                raise RuntimeError("Prospeo rate limit reached; no retry was attempted.") from error
            raise RuntimeError(f"Prospeo request failed with HTTP {error.code}.") from error
        except URLError as error:
            raise RuntimeError(f"Prospeo is unreachable: {error.reason}") from error
        return payload if isinstance(payload, dict) else {}
