from __future__ import annotations

from pathlib import Path

import pytest

from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.connectors.registry import ConnectorRegistry
from careeros.prompts import PromptLibrary


class HealthyConnector:
    name = "healthy"

    def healthcheck(self) -> None:
        return None


class UnhealthyConnector:
    name = "unhealthy"

    def healthcheck(self) -> None:
        raise ConnectorNotConfiguredError("not authenticated")


def test_connector_registry_is_explicit_and_fail_closed() -> None:
    registry = ConnectorRegistry()
    with pytest.raises(ConnectorNotConfiguredError, match="will not substitute mock"):
        registry.require("mail")
    connector = HealthyConnector()
    registry.register("mail", connector)
    assert registry.require("mail") is connector
    with pytest.raises(ValueError, match="already registered"):
        registry.register("mail", connector)

    deferred = ConnectorRegistry()
    deferred.register("mail", UnhealthyConnector(), validate=False)
    with pytest.raises(ConnectorNotConfiguredError, match="not authenticated"):
        deferred.require("mail")


def test_prompt_library_hashes_canonical_prompt() -> None:
    root = Path(__file__).resolve().parents[1] / "prompts"
    library = PromptLibrary(root)
    prompt = library.load("SYSTEM.md")
    assert "Never fabricate" in prompt.content
    assert len(prompt.sha256) == 64
    with pytest.raises(ValueError, match="escape"):
        library.load("../README.md")


def test_all_agent_instructions_preserve_mandatory_high_fit_review() -> None:
    root = Path(__file__).resolve().parents[1]
    instruction_paths = (
        root / "docs" / "agent-rules.md",
        root / "prompts" / "SYSTEM.md",
        root / "prompts" / "P02_JOB_DISCOVERY.md",
    )

    for path in instruction_paths:
        content = path.read_text(encoding="utf-8")
        assert "strictly greater than `0.75`" in content
        assert "mandatory_user_review" in content

    repository_rules = instruction_paths[0].read_text(encoding="utf-8")
    assert "reviewed candidate evidence" in repository_rules
    assert "eligibility is unknown" in repository_rules
    assert "disclosure, not approval" in repository_rules
