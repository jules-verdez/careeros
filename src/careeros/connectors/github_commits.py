"""Work addresses their owners published themselves, in public commit metadata.

Public commits may expose an author's configured email address. This connector reads
that metadata through GitHub's API and preserves a commit URL as evidence. It verifies
that the requested account is an organization before treating its repositories as
company sources.

Provenance here is unusually strong in one respect and unusually weak in another, and
both belong on the record rather than in a footnote.

Strong: the address is one the person typed into their own git configuration and pushed
to a public repository. It is observed, not inferred, so it does not carry the failure
mode of a pattern-built guess, and the ``.patch`` view of the commit is a permanent
public URL on which the address is plainly visible. That URL is what travels as the
citation :class:`PersonCandidate` requires.

Weak: publishing a commit is not an invitation to be contacted. Many people do not
realise their commit address is public at all. A company team page or a paper's
corresponding-author line is a better citation wherever one exists, and this connector
is the fallback for the case where none does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from careeros.connectors.base import ConnectorNotConfiguredError, PeopleDiscoveryConnector
from careeros.domain.records import PersonCandidate

API_ROOT = "https://api.github.com"
USER_AGENT = "careeros-github-commits"
ACCEPT = "application/vnd.github+json"

#: GitHub substitutes these for authors who enabled address privacy. They route nowhere.
PRIVATE_ADDRESS_SUFFIXES = ("users.noreply.github.com", "noreply.github.com")

#: Bot identities that commit constantly and answer nothing.
BOT_MARKERS = ("[bot]", "dependabot", "renovate", "github-actions", "semantic-release")

#: A commit address is observed rather than guessed, so it starts high. It is not 1.0:
#: people change employer without changing their git configuration, so the address can
#: be real and stale at the same time. Recency and deliverability need separate evidence.
OBSERVED_CONFIDENCE = 0.85

#: Commit volume evidences relevance to the work, not authority over hiring. The prior
#: stays neutral for the same reason Hunter's does: influence is a separate judgment.
NEUTRAL_INFLUENCE = 0.5


@dataclass
class CommitAuthor:
    """One person's public commit identity within an organisation."""

    email: str
    name: str
    login: str | None = None
    commits: int = 0
    repositories: set[str] = field(default_factory=set)
    #: Permanent public URL of one commit whose metadata shows this address.
    citation_url: str = ""

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", maxsplit=1)[-1].casefold()

    @property
    def is_usable(self) -> bool:
        """Whether the address is a real mailbox belonging to a person."""
        if not self.email or "@" not in self.email:
            return False
        if self.domain.endswith(PRIVATE_ADDRESS_SUFFIXES):
            return False
        haystack = f"{self.email} {self.name} {self.login or ''}".casefold()
        return not any(marker in haystack for marker in BOT_MARKERS)

    def role_summary(self) -> str:
        """An honest description of what is actually known: where they commit."""
        repositories = ", ".join(sorted(self.repositories)[:3])
        return f"Committer to {repositories}"[:255]


class GitHubCommitEmailConnector(PeopleDiscoveryConnector):
    """Reads public commit metadata for addresses, at no cost per result."""

    def __init__(self, *, timeout: float = 20.0, token: str | None = None) -> None:
        self._timeout = timeout
        self._token = token or os.environ.get("GITHUB_TOKEN") or self._token_from_cli()

    @property
    def name(self) -> str:
        return "github-commit-metadata"

    def healthcheck(self) -> None:
        """Confirm the API answers and report the remaining hourly budget."""
        if not self._token:
            msg = (
                "No GitHub credential. Set GITHUB_TOKEN, or run 'gh auth login' so the "
                "token can be read from the CLI. Unauthenticated callers get sixty "
                "requests an hour, which is not enough to sweep an organisation."
            )
            raise ConnectorNotConfiguredError(msg)
        try:
            self._get("rate_limit")
        except Exception as error:
            # Any failure here is a credential problem the operator must see.
            msg = f"GitHub rejected the credential: {error}"
            raise ConnectorNotConfiguredError(msg) from error

    def remaining_requests(self) -> int:
        """How many API calls the current credential may still make this hour."""
        payload = self._get("rate_limit")
        core = (payload.get("resources") or {}).get("core") or {}
        return int(core.get("remaining") or 0)

    def find_addresses(
        self,
        org: str,
        *,
        domain: str | None = None,
        max_repositories: int = 12,
        max_commits_per_repository: int = 100,
    ) -> tuple[CommitAuthor, ...]:
        """Collect commit authors for one organisation, most active first.

        ``domain`` is the filter that makes the result useful for outreach. An open-source
        repository owned by a fund carries commits from the fund's own engineers and from
        unaffiliated contributors alike, and only the former say anything about who works
        there. Passing the firm's mail domain keeps the people the search is actually for.
        """
        repositories = self._repositories(org, limit=max_repositories)
        found: dict[str, CommitAuthor] = {}
        for repository in repositories:
            for commit in self._commits(org, repository, limit=max_commits_per_repository):
                author = (commit.get("commit") or {}).get("author") or {}
                email = str(author.get("email") or "").strip().casefold()
                if not email:
                    continue
                account = commit.get("author") or {}
                record = found.get(email)
                if record is None:
                    record = CommitAuthor(
                        email=email,
                        name=str(author.get("name") or "").strip(),
                        login=str(account.get("login")) if account.get("login") else None,
                        citation_url=(
                            f"https://github.com/{org}/{repository}/commit/"
                            f"{commit.get('sha')}.patch"
                        ),
                    )
                    found[email] = record
                record.commits += 1
                record.repositories.add(f"{org}/{repository}")
        usable = [record for record in found.values() if record.is_usable]
        if domain:
            wanted = domain.strip().casefold().lstrip("@")
            usable = [
                record
                for record in usable
                if record.domain == wanted or record.domain.endswith(f".{wanted}")
            ]
        usable.sort(key=lambda record: (-record.commits, record.email))
        return tuple(usable)

    def discover_people(self, company_name: str) -> Iterable[PersonCandidate]:
        """Yield commit authors for ``company_name``, read as ``org`` or ``org:domain``.

        The two-part form is what callers should use. Without a domain every outside
        contributor to the organisation's repositories comes back too, and those people
        do not work there.
        """
        org, _, domain = company_name.partition(":")
        for record in self.find_addresses(org.strip(), domain=domain.strip() or None):
            if not record.name:
                continue
            yield PersonCandidate(
                full_name=record.name[:255],
                role=record.role_summary(),
                github_url=(
                    f"https://github.com/{record.login}" if record.login else None  # type: ignore[arg-type]
                ),
                public_email=record.email,
                public_email_source_url=record.citation_url,  # type: ignore[arg-type]
                influence_probability=NEUTRAL_INFLUENCE,
                confidence=OBSERVED_CONFIDENCE,
            )

    # ------------------------------------------------------------------- internals

    def _repositories(self, org: str, *, limit: int) -> list[str]:
        """List an organisation's own repositories, most recently pushed first.

        Forks are dropped: their commit history belongs to the upstream project, so
        scanning them attributes other people's addresses to this organisation.
        """
        payload = self._get(
            f"orgs/{quote(org)}/repos",
            sort="pushed",
            direction="desc",
            per_page=str(min(limit, 100)),
            type="sources",
        )
        if not isinstance(payload, list):
            return []
        return [
            str(item["name"])
            for item in payload
            if item.get("name") and not item.get("fork") and not item.get("archived")
        ][:limit]

    def _commits(self, org: str, repository: str, *, limit: int) -> list[dict[str, Any]]:
        payload = self._get(
            f"repos/{quote(org)}/{quote(repository)}/commits",
            per_page=str(min(limit, 100)),
        )
        return payload if isinstance(payload, list) else []

    def _get(self, path: str, **params: str) -> Any:
        url = f"{API_ROOT}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {"Accept": ACCEPT, "User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in (403, 429):
                msg = (
                    "GitHub rate-limited this credential. Authenticated callers get five "
                    "thousand requests an hour; wait for the window to reset."
                )
                raise RuntimeError(msg) from error
            if error.code == 404:
                return []
            raise RuntimeError(f"GitHub returned HTTP {error.code} for {path}") from error
        except URLError as error:
            raise RuntimeError(f"GitHub is unreachable: {error.reason}") from error

    @staticmethod
    def _token_from_cli() -> str | None:
        """Borrow the token the GitHub CLI already holds, rather than asking for a new one.

        Reading it is the same trust boundary as the ``gws`` credential the mail connector
        already uses: a locally installed tool the operator authenticated themselves.
        """
        if shutil.which("gh") is None:
            return None
        try:
            result = subprocess.run(
                ("gh", "auth", "token"),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        token = result.stdout.strip()
        return token or None
