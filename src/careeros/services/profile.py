"""Candidate profile and preference management."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.enums import PreferenceKind
from careeros.database.models import CandidatePreference, CandidateProfile
from careeros.services.audit import AuditService
from careeros.services.normalization import normalize_email, normalize_text


class ProfileService:
    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    def ensure_profile(
        self,
        session: Session,
        *,
        name: str,
        email: str | None = None,
        actor: str = "career.cli",
    ) -> CandidateProfile:
        profile = session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.created_at)
            .limit(1)
        )
        if profile is not None:
            if email is not None:
                profile.primary_email = normalize_email(email)
            return profile
        profile = CandidateProfile(
            name=name.strip(),
            primary_email=normalize_email(email) if email else None,
            is_active=True,
        )
        session.add(profile)
        session.flush()
        self._audit.append(
            session,
            actor=actor,
            event_type="candidate_profile.created",
            aggregate_type="candidate_profile",
            aggregate_id=profile.id,
            payload={"name": profile.name},
        )
        return profile

    def add_preference(
        self,
        session: Session,
        *,
        profile_id: str,
        kind: PreferenceKind,
        value: str,
        weight: float,
        is_required: bool,
        actor: str,
    ) -> CandidatePreference:
        normalized = normalize_text(value)
        existing = session.scalar(
            select(CandidatePreference).where(
                CandidatePreference.candidate_profile_id == profile_id,
                CandidatePreference.kind == kind,
                CandidatePreference.normalized_value == normalized,
            )
        )
        if existing is not None:
            existing.weight = weight
            existing.is_required = is_required
            preference = existing
            event_type = "candidate_preference.updated"
        else:
            preference = CandidatePreference(
                candidate_profile_id=profile_id,
                kind=kind,
                value=value.strip(),
                normalized_value=normalized,
                weight=weight,
                is_required=is_required,
            )
            session.add(preference)
            session.flush()
            event_type = "candidate_preference.created"
        self._audit.append(
            session,
            actor=actor,
            event_type=event_type,
            aggregate_type="candidate_preference",
            aggregate_id=preference.id,
            payload={
                "kind": kind.value,
                "value": preference.value,
                "weight": weight,
                "is_required": is_required,
            },
        )
        return preference
