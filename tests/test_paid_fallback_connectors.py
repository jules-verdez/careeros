from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from sqlalchemy import func, select

from careeros.connectors.brave import BraveSearchConnector, SearchSpendNotApprovedError
from careeros.connectors.prospeo import CreditSpendNotApprovedError, ProspeoConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.container import Container
from careeros.database.enums import ContactEnrichmentReason
from careeros.database.models import (
    Company,
    ContactEnrichmentObservation,
    PersonContact,
)
from careeros.services.contact_enrichment import ContactEnrichmentService
from careeros.services.normalization import normalize_text


def test_prospeo_requires_a_per_lookup_credit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROSPEO_API_KEY", "test-key")
    connector = ProspeoConnector(requester=lambda request, timeout: {})
    with pytest.raises(CreditSpendNotApprovedError, match="spend-one-credit"):
        connector.enrich_person(
            full_name="Sample Contact",
            company_name="Example Capital",
            company_website="example.test",
            linkedin_url=None,
            allow_credit_spend=False,
        )


def test_prospeo_requests_verified_email_only_and_never_mobile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROSPEO_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    def requester(request: Request, timeout: float) -> dict[str, Any]:
        assert timeout == 30.0
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads((request.data or b"{}").decode())
        return {
            "error": False,
            "free_enrichment": False,
            "person": {
                "full_name": "Sample Contact",
                "current_job_title": "Head of Quant",
                "linkedin_url": "https://www.linkedin.com/in/contact-example",
                "email": {
                    "status": "VERIFIED",
                    "revealed": True,
                    "email": "Recipient@Example.test",
                },
            },
            "company": {"name": "Example Capital"},
        }

    result = ProspeoConnector(requester=requester).enrich_person(
        full_name="Sample Contact",
        company_name="Example Capital",
        company_website="example.test",
        linkedin_url=None,
        allow_credit_spend=True,
    )

    assert captured["body"]["only_verified_email"] is True
    assert captured["body"]["enrich_mobile"] is False
    assert result.email_address == "recipient@example.test"
    assert result.is_verified
    assert result.credits_spent == 1
    assert len(result.response_sha256) == 64


def test_brave_requires_one_request_authorization_and_bounds_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    calls: list[Request] = []

    def requester(request: Request, timeout: float) -> dict[str, Any]:
        assert timeout == 20.0
        calls.append(request)
        return {
            "web": {
                "results": [
                    {
                        "title": "Official team page",
                        "url": "https://example.test/team",
                        "description": "Public biographies",
                        "page_age": "2 days ago",
                    }
                ]
            }
        }

    connector = BraveSearchConnector(requester=requester)
    with pytest.raises(SearchSpendNotApprovedError, match="spend-one-request"):
        connector.search(
            "site:example.test quant team",
            count=10,
            country="GB",
            allow_request_spend=False,
        )
    results = connector.search(
        "site:example.test quant team",
        count=10,
        country="GB",
        allow_request_spend=True,
    )

    assert len(calls) == 1
    query = parse_qs(urlsplit(calls[0].full_url).query)
    assert query["count"] == ["10"]
    assert query["country"] == ["GB"]
    assert results[0].url.encoded_string() == "https://example.test/team"


def test_vendor_observation_is_persisted_but_not_promoted_to_public_contact(
    container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROSPEO_API_KEY", "test-key")
    with container.database.session() as session:
        company = Company(
            canonical_name="Example Capital",
            normalized_name=normalize_text("Example Capital"),
            domain="example.test",
        )
        session.add(company)
        session.flush()
        person = container.people.add_public_person(
            session,
            company_id=company.id,
            full_name="Sample Contact",
            title="Head of Quant",
            influence_probability=0.9,
            confidence=0.8,
            linkedin_url="https://www.linkedin.com/in/contact-example",
            email=None,
            email_source_url=None,
            actor="test",
        )
        person_id = person.id

    connector = ProspeoConnector(
        requester=lambda request, timeout: {
            "error": False,
            "free_enrichment": True,
            "person": {
                "full_name": "Sample Contact",
                "email": {
                    "status": "VERIFIED",
                    "revealed": True,
                    "email": "recipient@example.test",
                },
            },
        }
    )
    registry = ConnectorRegistry()
    registry.register("contact_enrichment.prospeo", connector)
    service = ContactEnrichmentService(
        container.database,
        registry,
        container.clock,
        container.audit,
    )

    outcome = service.enrich(
        person_id,
        capability="contact_enrichment.prospeo",
        reason=ContactEnrichmentReason.HUNTER_ACCEPT_ALL,
        allow_credit_spend=True,
        actor="test",
    )

    assert outcome.result.email_address == "recipient@example.test"
    assert outcome.result.credits_spent == 0
    with container.database.session() as session:
        observation = session.get(ContactEnrichmentObservation, outcome.observation_id)
        public_contacts = session.scalar(select(func.count(PersonContact.id)))
        assert observation is not None
        assert observation.fallback_reason is ContactEnrichmentReason.HUNTER_ACCEPT_ALL
        assert public_contacts == 0
