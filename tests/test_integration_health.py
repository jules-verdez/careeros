from __future__ import annotations

import json
from pathlib import Path

from careeros.services.integration_health import (
    CommandResult,
    IntegrationHealthService,
    Readiness,
)


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...], *, timeout: float = 15.0) -> CommandResult:
        del timeout
        self.calls.append(args)
        return self.responses[args]


def test_google_live_probe_requires_valid_scopes_and_reads_only_profile(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")  # type: ignore[attr-defined]
    status = {
        "encryption_valid": True,
        "has_refresh_token": True,
        "token_valid": True,
        "user": "candidate@example.com",
        "scopes": [
            IntegrationHealthService.GMAIL_SCOPE,
            IntegrationHealthService.CALENDAR_SCOPE,
        ],
    }
    runner = FakeRunner(
        {
            ("gws", "auth", "status"): CommandResult(0, json.dumps(status), ""),
            (
                "gws",
                "gmail",
                "users",
                "getProfile",
                "--params",
                '{"userId":"me"}',
            ): CommandResult(
                0,
                json.dumps({"emailAddress": "candidate@example.com"}),
                "",
            ),
        }
    )
    result = IntegrationHealthService(runner, home=tmp_path).check_google_workspace(live=True)
    assert result.readiness is Readiness.READY
    assert result.identity == "candidate@example.com"
    assert len(runner.calls) == 2


_CODEX_HAS_APIFY = "Name Url Status Auth\napify https://mcp.apify.com enabled OAuth\n"


def test_apify_probes_distinguish_mcp_connection_from_optional_cli_login(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")  # type: ignore[attr-defined]
    runner = FakeRunner(
        {
            ("claude", "mcp", "list"): CommandResult(
                0,
                "apify: https://mcp.apify.com (HTTP) - ✔ Connected\n",
                "",
            ),
            ("apify", "--version"): CommandResult(0, "apify-cli/1.7.1\n", ""),
        }
    )
    service = IntegrationHealthService(runner, home=tmp_path)
    assert service.check_apify_mcp().readiness is Readiness.READY
    cli = service.check_apify_cli()
    assert cli.readiness is Readiness.PARTIAL
    assert "separate CLI login" in cli.detail


def test_apify_mcp_registered_only_in_codex_is_not_ready(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """The migration trap: Claude Code cannot reach a server registered in Codex."""
    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")  # type: ignore[attr-defined]
    runner = FakeRunner(
        {
            ("claude", "mcp", "list"): CommandResult(0, "vercel: https://x - connected\n", ""),
            ("codex", "mcp", "list"): CommandResult(0, _CODEX_HAS_APIFY, ""),
        }
    )
    result = IntegrationHealthService(runner, home=tmp_path).check_apify_mcp()
    assert result.readiness is Readiness.PARTIAL
    assert "claude mcp add" in result.detail


def test_apify_mcp_registered_but_unauthenticated_is_partial(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")  # type: ignore[attr-defined]
    runner = FakeRunner(
        {
            ("claude", "mcp", "list"): CommandResult(
                0,
                "apify: https://mcp.apify.com (HTTP) - ! Needs authentication\n",
                "",
            ),
        }
    )
    result = IntegrationHealthService(runner, home=tmp_path).check_apify_mcp()
    assert result.readiness is Readiness.PARTIAL
    assert "/mcp" in result.detail


def test_paid_readiness_probes_never_make_a_billable_call(monkeypatch: object) -> None:
    monkeypatch.setenv("PROSPEO_API_KEY", "present")  # type: ignore[attr-defined]
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "present")  # type: ignore[attr-defined]

    service = IntegrationHealthService()

    assert service.check_prospeo().readiness is Readiness.PARTIAL
    assert "never spends" in service.check_prospeo().detail
    assert service.check_brave_search().readiness is Readiness.PARTIAL
    assert "never spends" in service.check_brave_search().detail
    assert service.check_ukvi_register().readiness is Readiness.READY
