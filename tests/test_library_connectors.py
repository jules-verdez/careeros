"""Standalone connector contracts, without the retired chat interface."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from careeros.connectors import clay as clay_module
from careeros.connectors import explorium as explorium_module
from careeros.connectors.apify import ApifyActorRunner, ApifyBudgetExceededError
from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.connectors.clay import ClayWebhookConnector
from careeros.connectors.explorium import ExploriumConnector, contact_credits


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APIFY_TOKEN",
        "EXPLORIUM_API_KEY",
        "CLAY_WEBHOOK_URL",
        "CLAY_WEBHOOK_TOKEN",
        "CLAY_WEBHOOK_AUTH_HEADER",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_vibe_prospecting_requests_use_the_documented_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []
    responses = [
        {"matched_businesses": [{"business_id": "b" * 32}]},
        {"data": [{"prospect_id": "p" * 40, "full_name": "Sample", "extra": "dropped"}]},
        {
            "data": {
                "professions_email": "recipient@example.test",
                "emails": [],
                "mobile_phone": "+1",
            }
        },
        {"remaining_credits": 10, "allocated_credits": 20},
    ]

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        sent.append(request)
        return FakeResponse(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(explorium_module, "urlopen", fake_urlopen)
    connector = ExploriumConnector(api_key="secret")
    assert connector.match_business(name="Example", domain="example.test") == "b" * 32
    prospects = connector.fetch_prospects(
        business_ids=("b" * 32,), job_titles=("Head of Research",), country_codes=("gb",), limit=5
    )
    assert prospects == [
        {
            field: ({"prospect_id": "p" * 40, "full_name": "Sample"}).get(field)
            for field in explorium_module.PROSPECT_FIELDS
        }
    ]
    details = connector.contact_information("p" * 40)
    assert (
        details["professional_email"] == "recipient@example.test" and "mobile_phone" not in details
    )
    assert connector.credits() == {"remaining_credits": 10, "allocated_credits": 20}

    assert sent[0].full_url == "https://api.explorium.ai/v1/businesses/match"
    assert sent[0].get_header("Api_key") == "secret"
    body = json.loads(sent[1].data)
    assert body["filters"]["country_code"] == {"values": ["GB"]}
    assert body["filters"]["job_title"]["include_related_job_titles"] is True
    assert json.loads(sent[2].data)["parameters"] == {"contact_types": ["email"]}
    assert sent[3].get_method() == "GET"
    assert contact_credits(("email",)) == 2 and contact_credits(("email", "phone")) == 5
    with pytest.raises(ValueError):
        connector.fetch_prospects()
    with pytest.raises(ValueError):
        connector.contact_information("p" * 40, contact_types=("fax",))
    with pytest.raises(ConnectorNotConfiguredError):
        ExploriumConnector(api_key="").healthcheck()


def test_clay_sends_one_submission_per_row(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        sent.append(request)
        return FakeResponse(b"{}")

    monkeypatch.setattr(clay_module, "urlopen", fake_urlopen)
    connector = ClayWebhookConnector(url="https://api.clay.com/hook/abc", token="tok")
    assert connector.send_rows([{"name": "Sample"}, {"name": "Bo"}]) == {
        "rows_sent": 2,
        "destination": "api.clay.com",
    }
    assert json.loads(sent[1].data) == {"name": "Bo"}
    assert sent[0].get_header("X-clay-webhook-auth") == "tok"
    with pytest.raises(ValueError):
        connector.send_rows([])
    with pytest.raises(ConnectorNotConfiguredError, match="https"):
        ClayWebhookConnector(url="http://insecure.example").healthcheck()
    with pytest.raises(ConnectorNotConfiguredError):
        ClayWebhookConnector(url="").healthcheck()


def test_apify_runner_refuses_a_cap_larger_than_what_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ApifyActorRunner(token="t")
    monkeypatch.setattr(
        runner,
        "budget",
        lambda: {"monthly_limit_usd": 40.0, "used_usd": 39.0, "remaining_usd": 1.0},
    )
    with pytest.raises(ApifyBudgetExceededError, match=r"only \$1.00 remains"):
        runner.run("a/b", {}, max_charge_usd=2.0, max_items=10)
    with pytest.raises(ValueError):
        runner.run("a/b", {}, max_charge_usd=0, max_items=10)
    with pytest.raises(ConnectorNotConfiguredError):
        ApifyActorRunner(token="").healthcheck()
