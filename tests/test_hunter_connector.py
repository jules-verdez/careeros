from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.connectors.hunter import HunterConnector
from careeros.services.integration_health import IntegrationHealthService, Readiness


def _connector(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> HunterConnector:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    connector = HunterConnector()
    monkeypatch.setattr(connector, "_get", lambda endpoint, **params: payload)
    return connector


def test_missing_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    connector = HunterConnector()
    with pytest.raises(ConnectorNotConfiguredError):
        connector.healthcheck()
    with pytest.raises(ConnectorNotConfiguredError):
        list(connector.discover_people("example.test"))


def test_accept_all_domain_is_not_sendable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catch-all domain: the domain answers 250 to everything, so no verdict exists."""
    connector = _connector(
        monkeypatch,
        {"email": "recipient@example.test", "status": "accept_all", "score": 70},
    )
    verdict = connector.verify_email("recipient@example.test")
    assert verdict.is_unverifiable is True
    assert verdict.is_sendable is False


def test_valid_address_is_sendable(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(
        monkeypatch,
        {
            "email": "someone@example.com",
            "status": "valid",
            "score": 97,
            "sources": [{"uri": "https://example.com/team"}],
        },
    )
    verdict = connector.find_email(domain="example.com", first_name="Some", last_name="One")
    assert verdict.is_sendable is True
    assert verdict.source_url == "https://example.com/team"


def test_invalid_address_is_neither_sendable_nor_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _connector(monkeypatch, {"email": None, "status": "invalid", "score": 0})
    verdict = connector.verify_email("recipient@example.test")
    assert verdict.is_sendable is False
    assert verdict.is_unverifiable is False


def test_uncited_email_is_dropped_but_the_person_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PersonCandidate refuses an uncited email, so the address goes, not the lead."""
    connector = _connector(
        monkeypatch,
        {
            "emails": [
                {
                    "value": "cited@example.com",
                    "first_name": "Cited",
                    "last_name": "Person",
                    "position": "Head of Research",
                    "confidence": 94,
                    "sources": [{"uri": "https://example.com/about"}],
                },
                {
                    "value": "uncited@example.com",
                    "first_name": "Uncited",
                    "last_name": "Person",
                    "confidence": 50,
                    "sources": [],
                },
            ]
        },
    )
    people = list(connector.discover_people("example.com"))
    assert len(people) == 2

    cited, uncited = people
    assert cited.public_email == "cited@example.com"
    assert str(cited.public_email_source_url) == "https://example.com/about"
    assert cited.confidence == pytest.approx(0.94)

    assert uncited.public_email is None
    assert uncited.public_email_source_url is None
    assert uncited.full_name == "Uncited Person"


def _envelope_connector(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> HunterConnector:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    connector = HunterConnector()
    monkeypatch.setattr(connector, "_get_envelope", lambda endpoint, **params: body)
    return connector


def test_domain_search_reports_the_full_count_not_the_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counting the returned rows would understate a large domain and misjudge it."""
    connector = _envelope_connector(
        monkeypatch,
        {
            "data": {
                "domain": "example.test",
                "organization": "Example Capital",
                "pattern": "{first}.{last}",
                "accept_all": True,
                "webmail": False,
                "emails": [
                    {
                        "value": "info@example.test",
                        "type": "generic",
                        "confidence": 90,
                        "sources": [{"uri": "https://example.test/contact"}],
                    },
                    {
                        "value": "recipient@example.test",
                        "type": "personal",
                        "first_name": "Sample",
                        "last_name": "Contact",
                        "position": "Quantitative Researcher",
                        "confidence": 88,
                        "sources": [{"uri": "https://example.test/team"}],
                    },
                ],
            },
            "meta": {"results": 200, "limit": 2},
        },
    )
    profile = connector.domain_search("example.test", limit=2)

    assert profile.total_known == 200
    assert profile.has_pattern is True
    # A catch-all domain accepts every recipient, so the pattern stays a guess.
    assert profile.is_catch_all is True

    assert len(profile.contacts) == 2
    named = profile.named_contacts
    assert len(named) == 1
    assert named[0].email == "recipient@example.test"
    assert named[0].full_name == "Sample Contact"
    assert named[0].source_url == "https://example.test/team"


def test_domain_search_on_an_unknown_domain_is_reachable_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A firm Hunter has never seen cannot be reached by spending more on a scraper."""
    connector = _envelope_connector(monkeypatch, {"data": {"domain": "example.test"}, "meta": {}})
    profile = connector.domain_search("example.test")

    assert profile.total_known == 0
    assert profile.has_pattern is False
    assert profile.named_contacts == ()


@pytest.mark.parametrize(
    ("code", "detail", "expected"),
    [
        (401, "boom", ConnectorNotConfiguredError),
        (402, "boom", ConnectorNotConfiguredError),
        (429, "Too many requests", RuntimeError),
        (500, "boom", RuntimeError),
        # Hunter answers 429 for an account restriction too, but that one never
        # clears by waiting, so it must fail closed rather than invite a retry.
        (429, "Your account was restricted.", ConnectorNotConfiguredError),
    ],
)
def test_http_status_maps_to_configuration_or_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    detail: str,
    expected: type[Exception],
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    body = json.dumps({"errors": [{"details": detail}]}).encode("utf-8")

    def raise_http(*args: Any, **kwargs: Any) -> None:
        raise HTTPError("https://api.hunter.io", code, "err", {}, io.BytesIO(body))  # type: ignore[arg-type]

    monkeypatch.setattr("careeros.connectors.hunter.urlopen", raise_http)
    with pytest.raises(expected):
        HunterConnector().account()


def test_doctor_reports_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    result = IntegrationHealthService().check_hunter()
    assert result.readiness is Readiness.MISSING


def test_doctor_reports_remaining_credits_without_leaking_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "super-secret-value")
    service = IntegrationHealthService()
    monkeypatch.setattr(
        service,
        "_hunter_account",
        lambda: {
            "email": "candidate@example.com",
            "requests": {"searches": {"available": 2000, "used": 40}},
        },
    )
    result = service.check_hunter(live=True)
    assert result.readiness is Readiness.READY
    assert "1960" in result.detail
    assert "super-secret-value" not in result.detail
    assert result.identity == "candidate@example.com"
