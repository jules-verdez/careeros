"""Explicit eligibility evidence, kept separate from professional fit."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class EligibilityLabel(StrEnum):
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    CONFLICTING = "conflicting"
    CURRENTLY_INELIGIBLE = "currently_ineligible"


class ReviewEvidence(BaseModel):
    """A human-supplied assessment, not an inference from a sponsor licence.

    Confirmed means the reviewer checked the candidate's eligibility for this
    vacancy, including work authorization. Other material concerns may remain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    eligibility_label: EligibilityLabel = EligibilityLabel.UNKNOWN
    unresolved_fact: str = ""
    evidence: tuple[str, ...] = ()
    next_verification_step: str = Field(min_length=1)
    canonical_url: HttpUrl

    @model_validator(mode="after")
    def require_evidence_for_conclusions(self) -> Self:
        if any(not item.strip() for item in self.evidence):
            raise ValueError("evidence entries must not be blank")
        if self.eligibility_label != EligibilityLabel.UNKNOWN and not self.evidence:
            raise ValueError("eligibility conclusions require recorded evidence")
        if (
            self.eligibility_label
            in {
                EligibilityLabel.CONFLICTING,
                EligibilityLabel.CURRENTLY_INELIGIBLE,
            }
            and not self.unresolved_fact
        ):
            raise ValueError("adverse or conflicting evidence requires an unresolved_fact")
        return self


def text_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def evidence_items(value: Any) -> tuple[str, ...]:
    """Preserve legacy evidence text and structured citations without interpreting it."""

    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, dict):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True),) if value else ()
    if isinstance(value, (list, tuple)):
        return tuple(item for entry in value for item in evidence_items(entry))
    return ()


def legacy_eligibility(data: dict[str, Any], *, location: str | None = None) -> EligibilityLabel:
    """Unknown strings stay unknown; missing evidence never means ineligible."""

    del location  # Geography alone does not establish a candidate's work rights.

    value = text_value(data, "eligibility_label", "eligibility", "eligibility_status").lower()
    if value in {"currently_ineligible", "currently_ineligible_cohort", "ineligible"}:
        return EligibilityLabel.CURRENTLY_INELIGIBLE
    if value in {"conflicting", "cohort_conflict_requires_user_review"}:
        return EligibilityLabel.CONFLICTING
    # A historical sponsorship flag is not evidence that the candidate can work
    # without sponsorship. Only a complete, structured review can confirm eligibility.
    return EligibilityLabel.UNKNOWN
