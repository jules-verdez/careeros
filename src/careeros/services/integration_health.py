"""Read-only integration readiness probes.

These probes never print tokens, read message contents, or run Apify Actors. They
differentiate a binary being installed from credentials actually being usable.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from careeros.process import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)

# Re-exported: these moved to careeros.process so connectors can drive a vendor CLI
# without importing the services layer, but callers still import them from here.
__all__ = [
    "CommandResult",
    "CommandRunner",
    "IntegrationHealthService",
    "ProbeResult",
    "Readiness",
    "SubprocessCommandRunner",
]


class Readiness(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    integration: str
    readiness: Readiness
    detail: str
    identity: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        payload = asdict(self)
        payload["readiness"] = self.readiness.value
        return payload


class IntegrationHealthService:
    GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
    GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
    GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
    CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
    CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        home: Path | None = None,
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._home = home or Path.home()

    def check_all(self, *, live: bool = False) -> tuple[ProbeResult, ...]:
        return (
            self.check_apify_mcp(),
            self.check_apify_cli(),
            self.check_google_workspace(live=live),
            self.check_hunter(live=live),
            self.check_prospeo(),
            self.check_brave_search(),
            self.check_ukvi_register(),
        )

    @staticmethod
    def check_prospeo() -> ProbeResult:
        if not os.environ.get("PROSPEO_API_KEY"):
            return ProbeResult(
                integration="Prospeo",
                readiness=Readiness.MISSING,
                detail="PROSPEO_API_KEY is not set; Hunter fallback is unavailable.",
            )
        return ProbeResult(
            integration="Prospeo",
            readiness=Readiness.PARTIAL,
            detail=(
                "PROSPEO_API_KEY is present. Doctor never spends a credit; "
                "use person enrich with --spend-one-credit for a live check."
            ),
        )

    @staticmethod
    def check_brave_search() -> ProbeResult:
        if not os.environ.get("BRAVE_SEARCH_API_KEY"):
            return ProbeResult(
                integration="Brave Search",
                readiness=Readiness.MISSING,
                detail="BRAVE_SEARCH_API_KEY is not set; API evidence search is unavailable.",
            )
        return ProbeResult(
            integration="Brave Search",
            readiness=Readiness.PARTIAL,
            detail=(
                "BRAVE_SEARCH_API_KEY is present. Doctor never spends a request; "
                "use evidence search with --spend-one-request for a live check."
            ),
        )

    @staticmethod
    def check_ukvi_register() -> ProbeResult:
        return ProbeResult(
            integration="UKVI sponsor register",
            readiness=Readiness.READY,
            detail="Official public CSV connector is configured; doctor does not download it.",
        )

    def check_hunter(self, *, live: bool = False) -> ProbeResult:
        """Report whether outreach addresses can be verified before they are sent.

        Without this, contact addresses are name patterns rather than observed facts,
        so the probe treats a missing key as a real gap rather than an absent extra.
        """
        if not os.environ.get("HUNTER_API_KEY"):
            return ProbeResult(
                integration="Hunter.io",
                readiness=Readiness.MISSING,
                detail="HUNTER_API_KEY is not set; outreach addresses cannot be verified.",
            )
        if not live:
            return ProbeResult(
                integration="Hunter.io",
                readiness=Readiness.PARTIAL,
                detail="HUNTER_API_KEY is present; re-run with --live to confirm it works.",
            )
        try:
            account = self._hunter_account()
        except Exception as error:  # a probe reports failure, it never raises
            return ProbeResult(
                integration="Hunter.io",
                readiness=Readiness.ERROR,
                detail=f"Live Hunter.io check failed: {error}",
            )

        available = account.get("requests", {}).get("searches", {}).get("available")
        used = account.get("requests", {}).get("searches", {}).get("used")
        remaining = "unknown" if available is None else f"{available - (used or 0)}"
        return ProbeResult(
            integration="Hunter.io",
            readiness=Readiness.READY,
            detail=f"Key is valid; {remaining} search credits remaining this period.",
            identity=account.get("email"),
        )

    def _hunter_account(self) -> dict[str, Any]:
        from careeros.connectors.hunter import HunterConnector

        return HunterConnector().account()

    def check_apify_mcp(self) -> ProbeResult:
        """Report the Apify MCP as the *host running this pipeline* sees it.

        This probe used to read ``codex mcp list`` only. A server registered in Codex is
        invisible to Claude Code, so once the pipeline moved hosts the probe kept
        reporting ready while no Actor could be reached from the session actually doing
        the work. Claude Code is therefore the authority, and Codex is consulted only to
        explain where a missing server went.
        """
        claude = self._probe_claude_mcp()
        if claude is not None:
            return claude
        codex = self._probe_codex_mcp()
        if codex is not None:
            return ProbeResult(
                integration="Apify MCP",
                readiness=Readiness.PARTIAL,
                detail=(
                    "Registered in Codex but not in Claude Code, which cannot reach it. "
                    "Run: claude mcp add --transport http apify https://mcp.apify.com"
                ),
            )
        return ProbeResult(
            integration="Apify MCP",
            readiness=Readiness.MISSING,
            detail="No Apify MCP server is registered in Claude Code or Codex.",
        )

    def _probe_claude_mcp(self) -> ProbeResult | None:
        """Classify Claude Code's Apify entry, or None when there is nothing to classify."""
        if shutil.which("claude") is None:
            return None
        result = self._runner.run(("claude", "mcp", "list"), timeout=60.0)
        line = self._find_line(result.stdout, "apify:")
        if line is None:
            return None
        if "connected" in line:
            return ProbeResult(
                integration="Apify MCP",
                readiness=Readiness.READY,
                detail="Registered in Claude Code and connected.",
            )
        if "authentication" in line or "auth" in line:
            return ProbeResult(
                integration="Apify MCP",
                readiness=Readiness.PARTIAL,
                detail="Registered in Claude Code but unauthenticated; run /mcp to log in.",
            )
        return ProbeResult(
            integration="Apify MCP",
            readiness=Readiness.ERROR,
            detail="Registered in Claude Code but the connection check failed.",
        )

    def _probe_codex_mcp(self) -> str | None:
        if shutil.which("codex") is None:
            return None
        result = self._runner.run(("codex", "mcp", "list"))
        if result.returncode != 0:
            return None
        return self._find_line(result.stdout, "apify ")

    @staticmethod
    def _find_line(stdout: str, prefix: str) -> str | None:
        """Return the normalized, lowercased line starting with ``prefix``, if present."""
        for raw in stdout.splitlines():
            normalized = " ".join(raw.casefold().split())
            if normalized.startswith(prefix):
                return normalized
        return None

    def check_apify_cli(self) -> ProbeResult:
        executable = shutil.which("apify")
        if executable is None:
            return ProbeResult(
                integration="Apify CLI",
                readiness=Readiness.MISSING,
                detail="The apify executable is not installed.",
            )
        version = self._runner.run(("apify", "--version"))
        version_text = (
            version.stdout.strip().splitlines()[0] if version.stdout.strip() else "installed"
        )
        credential_file = self._home / ".apify" / "auth.json"
        if not credential_file.is_file():
            return ProbeResult(
                integration="Apify CLI",
                readiness=Readiness.PARTIAL,
                detail=f"{version_text}; separate CLI login is not configured.",
            )
        return ProbeResult(
            integration="Apify CLI",
            readiness=Readiness.READY,
            detail=f"{version_text}; credential file is present.",
        )

    def check_google_workspace(self, *, live: bool = False) -> ProbeResult:
        if shutil.which("gws") is None:
            return ProbeResult(
                integration="Google Workspace",
                readiness=Readiness.MISSING,
                detail="The gws executable is not installed.",
            )
        status = self._runner.run(("gws", "auth", "status"))
        if status.returncode != 0:
            return ProbeResult(
                integration="Google Workspace",
                readiness=Readiness.ERROR,
                detail="gws auth status failed.",
            )
        try:
            payload = json.loads(status.stdout)
        except json.JSONDecodeError:
            return ProbeResult(
                integration="Google Workspace",
                readiness=Readiness.ERROR,
                detail="gws returned an unreadable authentication status.",
            )
        scopes = set(payload.get("scopes", []))
        identity = payload.get("user")
        valid = bool(
            payload.get("encryption_valid")
            and payload.get("has_refresh_token")
            and payload.get("token_valid")
        )
        gmail_ready = self.GMAIL_SCOPE in scopes or {
            self.GMAIL_COMPOSE_SCOPE,
            self.GMAIL_READONLY_SCOPE,
        }.issubset(scopes)
        calendar_ready = self.CALENDAR_SCOPE in scopes or self.CALENDAR_READONLY_SCOPE in scopes
        if not valid or not gmail_ready or not calendar_ready:
            return ProbeResult(
                integration="Google Workspace",
                readiness=Readiness.PARTIAL,
                detail=("OAuth exists but Gmail draft/read or Calendar read access is incomplete."),
                identity=identity,
            )
        if live:
            profile = self._runner.run(
                (
                    "gws",
                    "gmail",
                    "users",
                    "getProfile",
                    "--params",
                    '{"userId":"me"}',
                )
            )
            if profile.returncode != 0:
                return ProbeResult(
                    integration="Google Workspace",
                    readiness=Readiness.ERROR,
                    detail="OAuth is valid, but the live Gmail profile request failed.",
                    identity=identity,
                )
            try:
                identity = json.loads(profile.stdout).get("emailAddress", identity)
            except json.JSONDecodeError:
                return ProbeResult(
                    integration="Google Workspace",
                    readiness=Readiness.ERROR,
                    detail="The live Gmail profile response was unreadable.",
                    identity=identity,
                )
        return ProbeResult(
            integration="Google Workspace",
            readiness=Readiness.READY,
            detail=(
                "OAuth, Gmail draft/read, and Calendar read scopes are valid"
                + ("; live Gmail profile succeeded." if live else ".")
            ),
            identity=identity,
        )
