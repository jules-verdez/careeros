"""Explicit one-request Brave Search adapter for transient evidence discovery."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import HttpUrl

from careeros.connectors.base import ConnectorNotConfiguredError, WebSearchConnector
from careeros.domain.records import WebSearchResult

API_URL = "https://api.search.brave.com/res/v1/web/search"
JsonRequester = Callable[[Request, float], dict[str, Any]]


class SearchSpendNotApprovedError(RuntimeError):
    """Raised before a paid-capable search when its one-request flag was omitted."""


class BraveSearchConnector(WebSearchConnector):
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        requester: JsonRequester | None = None,
    ) -> None:
        self._key = os.environ.get("BRAVE_SEARCH_API_KEY")
        self._timeout = timeout
        self._requester = requester or self._request_json

    @property
    def name(self) -> str:
        return "brave-search"

    def healthcheck(self) -> None:
        if not self._key:
            raise ConnectorNotConfiguredError(
                "BRAVE_SEARCH_API_KEY environment variable is not set."
            )

    def search(
        self,
        query: str,
        *,
        count: int,
        country: str,
        allow_request_spend: bool,
    ) -> tuple[WebSearchResult, ...]:
        if not allow_request_spend:
            raise SearchSpendNotApprovedError(
                "Brave Search may spend one request; re-run with --spend-one-request."
            )
        self.healthcheck()
        query = query.strip()
        if not query:
            raise ValueError("search query cannot be empty")
        if not 1 <= count <= 20:
            raise ValueError("Brave result count must be between 1 and 20")
        parameters = {
            "q": query,
            "count": count,
            "country": country.upper(),
            "search_lang": "en",
            "safesearch": "moderate",
            "text_decorations": "false",
        }
        url = f"{API_URL}?{urlencode(parameters)}"
        request = Request(
            url,
            headers={
                "X-Subscription-Token": str(self._key),
                "Accept": "application/json",
                "User-Agent": "careeros-evidence-search",
            },
        )
        payload = self._requester(request, self._timeout)
        web = payload.get("web")
        web = web if isinstance(web, dict) else {}
        results = web.get("results")
        results = results if isinstance(results, list) else []
        parsed: list[WebSearchResult] = []
        for item in results:
            if not isinstance(item, dict) or not item.get("title") or not item.get("url"):
                continue
            parsed.append(
                WebSearchResult(
                    title=str(item["title"])[:500],
                    url=HttpUrl(str(item["url"])),
                    description=item.get("description") or None,
                    page_age=item.get("page_age") or item.get("age") or None,
                )
            )
        return tuple(parsed)

    @staticmethod
    def _request_json(request: Request, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code in (401, 403):
                raise ConnectorNotConfiguredError(
                    "Brave Search rejected BRAVE_SEARCH_API_KEY."
                ) from error
            if error.code == 429:
                raise RuntimeError(
                    "Brave Search rate limit reached; no retry was attempted."
                ) from error
            raise RuntimeError(f"Brave Search failed with HTTP {error.code}.") from error
        except URLError as error:
            raise RuntimeError(f"Brave Search is unreachable: {error.reason}") from error
        return payload if isinstance(payload, dict) else {}
