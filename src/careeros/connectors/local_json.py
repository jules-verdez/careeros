"""Explicit local-file connector for reproducible job ingestion."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from careeros.domain.records import JobDiscoveryQuery, JobRecord


class LocalJsonJobConnector:
    """Read exported job observations from a JSON file.

    This is a production connector for controlled imports, not a sample-data
    fallback. The input must be a JSON array or ``{"jobs": [...]}``.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def name(self) -> str:
        return "local-json"

    def healthcheck(self) -> None:
        if not self._path.is_file():
            msg = f"job import file does not exist: {self._path}"
            raise FileNotFoundError(msg)
        if self._path.suffix.lower() != ".json":
            msg = f"job import must be JSON: {self._path}"
            raise ValueError(msg)

    def discover(self, query: JobDiscoveryQuery) -> list[JobRecord]:
        self.healthcheck()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            msg = f"invalid JSON in {self._path}: {error}"
            raise ValueError(msg) from error
        items = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            msg = "job import root must be an array or an object containing a 'jobs' array"
            raise ValueError(msg)
        try:
            records = TypeAdapter(list[JobRecord]).validate_python(items)
        except ValidationError as error:
            msg = f"invalid job record in {self._path}: {error}"
            raise ValueError(msg) from error
        return records[: query.limit]
