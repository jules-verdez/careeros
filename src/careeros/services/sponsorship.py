"""Conservative company matching against the official UKVI sponsor register."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.connectors.sponsors import (
    SponsorRegisterEntry,
    SponsorRegisterSnapshot,
    normalize_sponsor_name,
)
from careeros.database.enums import SponsorMatchStatus
from careeros.database.models import (
    Company,
    CompanyAlias,
    CompanySponsorshipAssessment,
)
from careeros.database.session import Database
from careeros.services.audit import AuditService

LEGAL_SUFFIXES = frozenset(
    {
        "limited",
        "ltd",
        "llp",
        "plc",
        "inc",
        "incorporated",
        "corp",
        "corporation",
    }
)


@dataclass(frozen=True)
class SponsorshipSyncSummary:
    companies_checked: int
    matched: int
    probable_alias: int
    ambiguous: int
    not_found: int
    source_sha256: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "companies_checked": self.companies_checked,
            "matched": self.matched,
            "probable_alias": self.probable_alias,
            "ambiguous": self.ambiguous,
            "not_found": self.not_found,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class _Match:
    status: SponsorMatchStatus
    organisation: str | None
    candidates: list[dict[str, object]]
    basis: str
    score: float


class SponsorshipService:
    def __init__(self, database: Database, audit: AuditService) -> None:
        self._database = database
        self._audit = audit

    def sync(
        self,
        snapshot: SponsorRegisterSnapshot,
        *,
        actor: str,
        company_name: str | None = None,
    ) -> SponsorshipSyncSummary:
        exact, simplified = self._indexes(snapshot.entries)
        counts = dict.fromkeys(SponsorMatchStatus, 0)
        with self._database.session() as session:
            query = select(Company).order_by(Company.canonical_name)
            if company_name is not None:
                query = query.where(Company.normalized_name == normalize_sponsor_name(company_name))
            companies = session.scalars(query).all()
            if company_name is not None and not companies:
                raise LookupError(f"company not found: {company_name}")
            for company in companies:
                aliases = session.scalars(
                    select(CompanyAlias).where(CompanyAlias.company_id == company.id)
                ).all()
                match = self._match(company, aliases, exact=exact, simplified=simplified)
                counts[match.status] += 1
                assessment = session.get(CompanySponsorshipAssessment, company.id)
                if assessment is None:
                    assessment = CompanySponsorshipAssessment(company_id=company.id)
                    session.add(assessment)
                assessment.status = match.status
                assessment.matched_organisation = match.organisation
                assessment.candidate_organisations_json = match.candidates
                assessment.match_basis = match.basis
                assessment.match_score = match.score
                assessment.source_url = snapshot.source_url
                assessment.source_sha256 = snapshot.source_sha256
                assessment.assessed_at = snapshot.captured_at

            self._audit.append(
                session,
                actor=actor,
                event_type="sponsor_register.synchronized",
                aggregate_type="sponsor_register",
                aggregate_id=snapshot.source_sha256,
                payload={
                    "source_url": snapshot.source_url,
                    "companies_checked": len(companies),
                    **{status.value: counts[status] for status in SponsorMatchStatus},
                },
            )

        return SponsorshipSyncSummary(
            companies_checked=len(companies),
            matched=counts[SponsorMatchStatus.MATCHED],
            probable_alias=counts[SponsorMatchStatus.PROBABLE_ALIAS],
            ambiguous=counts[SponsorMatchStatus.AMBIGUOUS],
            not_found=counts[SponsorMatchStatus.NOT_FOUND],
            source_sha256=snapshot.source_sha256,
        )

    def add_alias(
        self,
        session: Session,
        *,
        company_id: str,
        licensed_name: str,
        actor: str,
    ) -> CompanyAlias:
        company = session.get(Company, company_id)
        if company is None:
            raise LookupError(f"company not found: {company_id}")
        normalized = normalize_sponsor_name(licensed_name)
        existing = session.scalar(
            select(CompanyAlias).where(CompanyAlias.normalized_alias == normalized)
        )
        if existing is not None:
            if existing.company_id != company.id:
                raise ValueError("that licensed name is already assigned to another company")
            return existing
        alias = CompanyAlias(
            company_id=company.id,
            alias=licensed_name.strip(),
            normalized_alias=normalized,
        )
        session.add(alias)
        session.flush()
        self._audit.append(
            session,
            actor=actor,
            event_type="company.sponsor_alias_added",
            aggregate_type="company",
            aggregate_id=company.id,
            payload={"licensed_name": licensed_name.strip()},
        )
        return alias

    @classmethod
    def _match(
        cls,
        company: Company,
        aliases: Sequence[CompanyAlias],
        *,
        exact: dict[str, tuple[SponsorRegisterEntry, ...]],
        simplified: dict[str, tuple[SponsorRegisterEntry, ...]],
    ) -> _Match:
        names = [(normalize_sponsor_name(company.canonical_name), "exact_name")]
        names.extend((alias.normalized_alias, "explicit_alias") for alias in aliases)
        for name, basis in names:
            entries = exact.get(name, ())
            if entries:
                return cls._resolved(entries, basis=basis, score=1.0)

        candidates: list[SponsorRegisterEntry] = []
        for name, _ in names:
            candidates.extend(simplified.get(cls._without_legal_suffix(name), ()))
        if candidates:
            return cls._resolved(tuple(candidates), basis="legal_suffix_only", score=0.9)
        return _Match(
            status=SponsorMatchStatus.NOT_FOUND,
            organisation=None,
            candidates=[],
            basis="no_name_match",
            score=0.0,
        )

    @classmethod
    def _resolved(
        cls,
        entries: tuple[SponsorRegisterEntry, ...],
        *,
        basis: str,
        score: float,
    ) -> _Match:
        collapsed = cls._collapse(entries)
        if len(collapsed) == 1:
            organisation = str(collapsed[0]["organisation_name"])
            status = (
                SponsorMatchStatus.MATCHED
                if basis in {"exact_name", "explicit_alias"}
                else SponsorMatchStatus.PROBABLE_ALIAS
            )
            return _Match(status, organisation, collapsed, basis, score)
        return _Match(
            SponsorMatchStatus.AMBIGUOUS,
            None,
            collapsed,
            f"ambiguous_{basis}",
            min(score, 0.5),
        )

    @staticmethod
    def _collapse(entries: tuple[SponsorRegisterEntry, ...]) -> list[dict[str, object]]:
        grouped: dict[str, list[SponsorRegisterEntry]] = defaultdict(list)
        for entry in entries:
            grouped[entry.organisation_name].append(entry)
        result: list[dict[str, object]] = []
        for name in sorted(grouped):
            rows = grouped[name]
            result.append(
                {
                    "organisation_name": name,
                    "towns": sorted({row.town_city for row in rows if row.town_city}),
                    "routes": sorted({row.route for row in rows if row.route}),
                    "licence_types": sorted(
                        {row.licence_type_rating for row in rows if row.licence_type_rating}
                    ),
                }
            )
        return result

    @classmethod
    def _indexes(
        cls,
        entries: tuple[SponsorRegisterEntry, ...],
    ) -> tuple[
        dict[str, tuple[SponsorRegisterEntry, ...]],
        dict[str, tuple[SponsorRegisterEntry, ...]],
    ]:
        exact_lists: dict[str, list[SponsorRegisterEntry]] = defaultdict(list)
        simplified_lists: dict[str, list[SponsorRegisterEntry]] = defaultdict(list)
        for entry in entries:
            exact_lists[entry.normalized_name].append(entry)
            simplified_lists[cls._without_legal_suffix(entry.normalized_name)].append(entry)
        return (
            {key: tuple(value) for key, value in exact_lists.items()},
            {key: tuple(value) for key, value in simplified_lists.items()},
        )

    @staticmethod
    def _without_legal_suffix(value: str) -> str:
        tokens = value.split()
        while tokens and tokens[-1] in LEGAL_SUFFIXES:
            tokens.pop()
        return " ".join(tokens)
