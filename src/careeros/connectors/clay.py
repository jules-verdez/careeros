"""Clay connector: send rows to a Clay table through the table's webhook source.

Clay has no public API for writing to tables; its documented route is a Webhook
source on the table, which accepts JSON over HTTP POST and creates one row per
submission (50,000 submissions per webhook outside Enterprise plans). An optional
authentication token travels in a header; Clay's setup screen shows the exact cURL.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from careeros.connectors.base import ConnectorNotConfiguredError

#: The header Clay's community answers name for webhook tokens. Confirm it against the
#: cURL command on the table's webhook setup screen; ``CLAY_WEBHOOK_AUTH_HEADER`` overrides it.
DEFAULT_AUTH_HEADER = "x-clay-webhook-auth"
MAX_ROWS_PER_CALL = 50


class ClayWebhookConnector:
    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        auth_header: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._url = url or os.environ.get("CLAY_WEBHOOK_URL")
        self._token = token or os.environ.get("CLAY_WEBHOOK_TOKEN")
        self._auth_header = (
            auth_header or os.environ.get("CLAY_WEBHOOK_AUTH_HEADER") or DEFAULT_AUTH_HEADER
        )
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "clay-webhook"

    @property
    def destination(self) -> str:
        return urlsplit(self._url).hostname or "" if self._url else ""

    def healthcheck(self) -> None:
        if not self._url:
            raise ConnectorNotConfiguredError(
                "CLAY_WEBHOOK_URL is not set; copy the webhook URL from your Clay table."
            )
        if urlsplit(self._url).scheme != "https":
            raise ConnectorNotConfiguredError("CLAY_WEBHOOK_URL must be an https URL.")

    def send_rows(self, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """POST each row as one webhook submission; stop at the first failure."""
        self.healthcheck()
        assert self._url is not None
        batch = list(rows)
        if not batch:
            raise ValueError("Send at least one row.")
        if len(batch) > MAX_ROWS_PER_CALL:
            raise ValueError(f"Send at most {MAX_ROWS_PER_CALL} rows at a time.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers[self._auth_header] = self._token
        sent = 0
        for row in batch:
            request = Request(
                self._url, data=json.dumps(row).encode(), headers=headers, method="POST"
            )
            try:
                with urlopen(request, timeout=self._timeout) as response:
                    response.read()
            except HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:200]
                raise RuntimeError(
                    f"Clay refused row {sent + 1} of {len(batch)} ({error.code}): {detail}. "
                    f"{sent} rows were sent before it."
                ) from error
            except URLError as error:
                raise RuntimeError(
                    f"Clay is unreachable after {sent} rows: {error.reason}"
                ) from error
            sent += 1
        return {"rows_sent": sent, "destination": self.destination}
