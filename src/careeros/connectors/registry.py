"""Typed connector registry for dependency injection."""

from __future__ import annotations

from typing import Any, TypeVar

from careeros.connectors.base import ConnectorNotConfiguredError

ConnectorT = TypeVar("ConnectorT")


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Any] = {}

    def register(self, capability: str, connector: Any, *, validate: bool = True) -> None:
        if capability in self._connectors:
            msg = f"connector capability already registered: {capability}"
            raise ValueError(msg)
        if validate:
            connector.healthcheck()
        self._connectors[capability] = connector

    def require(self, capability: str) -> Any:
        try:
            connector = self._connectors[capability]
        except KeyError as error:
            msg = (
                f"no connector configured for '{capability}'. "
                "CareerOS will not substitute mock external data."
            )
            raise ConnectorNotConfiguredError(msg) from error
        connector.healthcheck()
        return connector
