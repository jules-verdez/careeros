from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from careeros.connectors.sponsors import (
    REGISTER_PAGE_URL,
    SponsorRegisterEntry,
    SponsorRegisterSnapshot,
    UkSponsorRegisterConnector,
    normalize_sponsor_name,
)
from careeros.container import Container
from careeros.database.enums import SponsorMatchStatus
from careeros.database.models import Company, CompanySponsorshipAssessment
from careeros.services.normalization import normalize_text


def test_official_register_connector_follows_the_govuk_csv_and_hashes_it() -> None:
    asset = (
        "https://assets.publishing.service.gov.uk/media/example/"
        "SP_-_Worker_and_Temporary_Worker_Web_Register.csv"
    )
    page = f'<html><a href="{asset}">Register</a></html>'.encode()
    csv_body = (
        b"Organisation Name,Town/City,County,Type & Rating,Route\n"
        b"Acme Capital Limited,London,London,Worker (A rating),Skilled Worker\n"
        b"Acme Capital Limited,London,London,Worker (A rating),Global Business Mobility\n"
    )
    reads: list[str] = []

    def reader(url: str, timeout: float) -> bytes:
        assert timeout == 30.0
        reads.append(url)
        return page if url == REGISTER_PAGE_URL else csv_body

    as_of = datetime(2026, 8, 20, 12, tzinfo=UTC)
    snapshot = UkSponsorRegisterConnector(reader=reader).fetch(as_of=as_of)

    assert reads == [REGISTER_PAGE_URL, asset]
    assert snapshot.source_url == asset
    assert snapshot.captured_at == as_of
    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].route == "Skilled Worker"
    assert snapshot.entries[0].normalized_name == "acme capital limited"
    assert len(snapshot.source_sha256) == 64


def _entry(name: str, route: str = "Skilled Worker") -> SponsorRegisterEntry:
    return SponsorRegisterEntry(
        organisation_name=name,
        normalized_name=normalize_sponsor_name(name),
        town_city="London",
        county="London",
        licence_type_rating="Worker (A rating)",
        route=route,
    )


def test_sync_distinguishes_exact_alias_probable_and_missing_matches(
    container: Container,
) -> None:
    with container.database.session() as session:
        probable = Company(
            canonical_name="Acme Capital",
            normalized_name=normalize_text("Acme Capital"),
            domain="acme.test",
        )
        aliased = Company(
            canonical_name="Example RT",
            normalized_name=normalize_text("Example RT"),
            domain="research.example",
        )
        missing = Company(
            canonical_name="No Licence Analytics",
            normalized_name=normalize_text("No Licence Analytics"),
            domain="no-licence.test",
        )
        session.add_all([probable, aliased, missing])
        session.flush()
        container.sponsorships.add_alias(
            session,
            company_id=aliased.id,
            licensed_name="Example Research & Technologies Limited",
            actor="test",
        )
        probable_id, aliased_id, missing_id = probable.id, aliased.id, missing.id

    snapshot = SponsorRegisterSnapshot(
        entries=(
            _entry("Acme Capital Limited"),
            _entry("Example Research and Technologies Limited"),
            _entry("Example Research and Technologies Limited", "Global Business Mobility"),
        ),
        source_url="https://assets.publishing.service.gov.uk/register.csv",
        source_sha256="a" * 64,
        captured_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    summary = container.sponsorships.sync(snapshot, actor="test")

    assert summary.matched == 1
    assert summary.probable_alias == 1
    assert summary.not_found == 1
    with container.database.session() as session:
        probable_result = session.get(CompanySponsorshipAssessment, probable_id)
        aliased_result = session.get(CompanySponsorshipAssessment, aliased_id)
        missing_result = session.get(CompanySponsorshipAssessment, missing_id)
        assert probable_result is not None
        assert probable_result.status is SponsorMatchStatus.PROBABLE_ALIAS
        assert probable_result.match_basis == "legal_suffix_only"
        assert aliased_result is not None
        assert aliased_result.status is SponsorMatchStatus.MATCHED
        assert aliased_result.match_basis == "explicit_alias"
        assert len(aliased_result.candidate_organisations_json[0]["routes"]) == 2
        assert missing_result is not None
        assert missing_result.status is SponsorMatchStatus.NOT_FOUND

        stored = session.scalars(select(CompanySponsorshipAssessment)).all()
        assert len(stored) == 3
