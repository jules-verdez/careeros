"""The free contact boundaries: DNS and SMTP verdicts, commit metadata, published pages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from careeros.clock import FixedClock
from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.connectors.deliverability import DeliverabilityConnector
from careeros.connectors.github_commits import GitHubCommitEmailConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.connectors.team_pages import CompanyPageContactConnector
from careeros.container import Container
from careeros.database.enums import ContactDeliveryStatus
from careeros.database.models import Company, PersonContact
from careeros.services.deliverability import DeliverabilityService
from careeros.services.normalization import normalize_text

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def _mx_answer(*hosts: str) -> dict[str, Any]:
    return {
        "Status": 0,
        "Answer": [{"type": 15, "data": f"10 {host}."} for host in hosts],
    }


class StubDeliverability(DeliverabilityConnector):
    """Exercise the connector with its two network calls replaced."""

    def __init__(self, *, dns: dict[str, dict[str, Any]], smtp: dict[str, tuple[int, str]]) -> None:
        super().__init__(clock=FixedClock(NOW))
        self._dns = dns
        self._smtp = smtp
        self.probes: list[str] = []

    def healthcheck(self) -> None:
        """The stub holds no network, so readiness is not in question."""
        return None

    def _resolve(self, name: str, record_type: int) -> dict[str, Any]:
        del record_type
        if name not in self._dns:
            raise RuntimeError(f"resolver stub has no answer for {name}")
        return self._dns[name]

    def _rcpt(self, exchanger: str, address: str) -> tuple[int, str]:
        del exchanger
        self.probes.append(address)
        for pattern, reply in self._smtp.items():
            if pattern == "*" and "@" in address:
                continue
            if address == pattern:
                return reply
        return self._smtp.get("*", (0, "no stub reply"))


def test_domain_without_a_mail_exchanger_is_disproved_without_any_probe() -> None:
    connector = StubDeliverability(dns={"example.test": {"Status": 0}}, smtp={})
    verdict = connector.check("recipient@example.test")
    assert verdict.status is ContactDeliveryStatus.INVALID
    assert verdict.method == "dns_mx"
    assert verdict.is_disproved
    assert connector.probes == []


def test_missing_domain_is_disproved() -> None:
    connector = StubDeliverability(dns={"gone.test": {"Status": 3}}, smtp={})
    verdict = connector.check("someone@gone.test")
    assert verdict.status is ContactDeliveryStatus.INVALID
    assert "does not exist" in verdict.detail


def test_dns_only_run_reports_unknown_rather_than_guessing() -> None:
    connector = StubDeliverability(dns={"acme.test": _mx_answer("mx.acme.test")}, smtp={})
    verdict = connector.check("recipient@acme.test")
    assert verdict.status is ContactDeliveryStatus.UNKNOWN
    assert verdict.has_mx is True
    assert verdict.is_catch_all is None
    assert connector.probes == []


def test_smtp_rejection_disproves_the_mailbox() -> None:
    connector = StubDeliverability(
        dns={"acme.test": _mx_answer("mx.acme.test")},
        smtp={"recipient@acme.test": (550, "5.1.1 user unknown"), "*": (550, "5.1.1 user unknown")},
    )
    verdict = connector.check("recipient@acme.test", allow_smtp_probe=True)
    assert verdict.status is ContactDeliveryStatus.INVALID
    assert verdict.method == "smtp_probe"
    assert verdict.is_catch_all is False


def test_acceptance_at_a_catch_all_domain_is_risky_not_deliverable() -> None:
    connector = StubDeliverability(
        dns={"example.test": _mx_answer("mx.example.test")},
        smtp={"*": (250, "2.1.5 ok")},
    )
    verdict = connector.check("recipient@example.test", allow_smtp_probe=True)
    assert verdict.status is ContactDeliveryStatus.RISKY
    assert verdict.is_catch_all is True
    assert "proves nothing" in verdict.detail


def test_acceptance_at_a_selective_domain_is_deliverable() -> None:
    connector = StubDeliverability(
        dns={"acme.test": _mx_answer("mx.acme.test")},
        smtp={"recipient@acme.test": (250, "2.1.5 ok"), "*": (550, "5.1.1 user unknown")},
    )
    verdict = connector.check("recipient@acme.test", allow_smtp_probe=True)
    assert verdict.status is ContactDeliveryStatus.DELIVERABLE
    assert verdict.is_catch_all is False
    assert verdict.is_sendable


def test_greylisting_is_reported_as_unknown_not_as_a_bad_address() -> None:
    connector = StubDeliverability(
        dns={"acme.test": _mx_answer("mx.acme.test")},
        smtp={"*": (451, "4.7.1 try again later")},
    )
    verdict = connector.check("recipient@acme.test", allow_smtp_probe=True)
    assert verdict.status is ContactDeliveryStatus.UNKNOWN
    assert verdict.method == "smtp_probe"


def test_a_resolver_outage_never_disproves_an_address() -> None:
    connector = StubDeliverability(dns={}, smtp={})
    verdict = connector.check("recipient@acme.test")
    assert verdict.status is ContactDeliveryStatus.UNKNOWN
    assert not verdict.is_disproved


class StubGitHub(GitHubCommitEmailConnector):
    def __init__(self, payloads: dict[str, Any]) -> None:
        super().__init__(token="test-token")
        self._payloads = payloads

    def _get(self, path: str, **params: str) -> Any:
        del params
        return self._payloads.get(path, [])


def _commit(sha: str, name: str, email: str, login: str | None = None) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"author": {"name": name, "email": email}},
        "author": {"login": login} if login else None,
    }


def _github_stub() -> StubGitHub:
    return StubGitHub(
        {
            "orgs/acme/repos": [
                {"name": "engine", "fork": False, "archived": False},
                {"name": "upstream-fork", "fork": True, "archived": False},
                {"name": "retired", "fork": False, "archived": True},
            ],
            "repos/acme/engine/commits": [
                _commit("a1", "Sample Contact", "recipient@acme.test", "contact"),
                _commit("a2", "Sample Contact", "recipient@acme.test", "contact"),
                _commit("b1", "Outside Helper", "helper@gmail.test"),
                _commit("c1", "dependabot[bot]", "bot@acme.test"),
                _commit("d1", "Private Person", "0000+example@users.noreply.github.com"),
            ],
        }
    )


def test_commit_authors_are_filtered_to_the_company_and_ranked_by_activity() -> None:
    found = _github_stub().find_addresses("acme", domain="acme.test")
    assert [record.email for record in found] == ["recipient@acme.test"]
    assert found[0].commits == 2
    assert found[0].repositories == {"acme/engine"}


def test_bots_and_privacy_addresses_are_never_returned() -> None:
    found = _github_stub().find_addresses("acme")
    emails = {record.email for record in found}
    assert "bot@acme.test" not in emails
    assert not any(email.endswith("users.noreply.github.com") for email in emails)
    assert "helper@gmail.test" in emails


def test_every_candidate_carries_a_permanent_public_citation() -> None:
    candidates = list(_github_stub().discover_people("acme:acme.test"))
    assert len(candidates) == 1
    citation = str(candidates[0].public_email_source_url)
    assert citation == "https://github.com/acme/engine/commit/a1.patch"
    assert candidates[0].public_email == "recipient@acme.test"


def test_github_connector_without_a_credential_fails_closed() -> None:
    connector = GitHubCommitEmailConnector(token="")
    connector._token = None
    with pytest.raises(ConnectorNotConfiguredError, match="GITHUB_TOKEN"):
        connector.healthcheck()


TEAM_PAGE = """
<html><body>
  <h3>Sample Contact</h3>
  <a href="mailto:recipient@acme.test">Sample Contact</a>
  <a href="mailto:info@acme.test">Get in touch</a>
  <a href="mailto:press@agency.test">Our press agency</a>
  <p>Reach the desk at desk@acme.test or see logo@2x.test</p>
</body></html>
"""


class StubPages(CompanyPageContactConnector):
    def __init__(self, pages: dict[str, str]) -> None:
        super().__init__(paths=("/team",))
        self._pages = pages
        self.fetched: list[str] = []

    def _fetch(self, url: str) -> str | None:
        self.fetched.append(url)
        return self._pages.get(url)


def test_published_addresses_cite_the_page_and_exclude_third_parties() -> None:
    connector = StubPages({"https://acme.test/team": TEAM_PAGE})
    found = connector.find_contacts("acme.test")
    emails = [contact.email for contact in found]
    assert "press@agency.test" not in emails
    assert "logo@2x.test" not in emails
    assert set(emails) == {"recipient@acme.test", "info@acme.test", "desk@acme.test"}
    contact = next(contact for contact in found if contact.email == "recipient@acme.test")
    assert contact.name == "Sample Contact"
    assert contact.page_url == "https://acme.test/team"
    assert contact.is_role_address is False
    assert next(c for c in found if c.email == "info@acme.test").is_role_address is True


def test_only_named_individuals_become_candidates() -> None:
    connector = StubPages({"https://acme.test/team": TEAM_PAGE})
    candidates = list(connector.discover_people("acme.test"))
    assert [candidate.full_name for candidate in candidates] == ["Sample Contact"]
    assert str(candidates[0].public_email_source_url) == "https://acme.test/team"


def test_a_disallowing_robots_file_stops_the_fetch() -> None:
    connector = StubPages(
        {
            "https://acme.test/robots.txt": "User-agent: *\nDisallow: /team\n",
            "https://acme.test/team": TEAM_PAGE,
        }
    )
    assert connector.find_contacts("acme.test") == ()
    assert "https://acme.test/team" not in connector.fetched


def _stored_contact(container: Container, email: str, source_url: str) -> str:
    with container.database.session() as session:
        company = Company(canonical_name="Acme", normalized_name=normalize_text("Acme"))
        session.add(company)
        session.flush()
        person = container.people.add_public_person(
            session,
            company_id=company.id,
            full_name="Sample Contact",
            title="Head of Quant",
            influence_probability=0.8,
            confidence=0.8,
            linkedin_url=None,
            email=email,
            email_source_url=source_url,
            actor="test",
        )
        contact = session.scalars(
            select_contact := __import__("sqlalchemy")
            .select(PersonContact)
            .where(PersonContact.person_id == person.id)
        ).one()
        del select_contact
        return contact.id


def _service(container: Container, **connectors: Any) -> DeliverabilityService:
    registry = ConnectorRegistry()
    for capability, connector in connectors.items():
        registry.register(capability.replace("__", "."), connector, validate=False)
    return DeliverabilityService(container.database, registry, container.clock, container.audit)


def test_recheck_marks_an_unreachable_domain_invalid_and_audits_it(
    container: Container,
) -> None:
    contact_id = _stored_contact(container, "recipient@dead.test", "https://dead.test/team")
    checker = StubDeliverability(dns={"dead.test": {"Status": 0}}, smtp={})
    results = _service(container, mail__deliverability=checker).recheck_contacts(actor="test")
    assert len(results) == 1
    assert results[0].changed is True
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        assert contact.delivery_status is ContactDeliveryStatus.INVALID
        assert contact.delivery_detail is not None


def test_recheck_never_overwrites_a_decided_status_with_not_asked(
    container: Container,
) -> None:
    contact_id = _stored_contact(container, "recipient@acme.test", "https://acme.test/team")
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        contact.delivery_status = ContactDeliveryStatus.DELIVERABLE
    checker = StubDeliverability(dns={"acme.test": _mx_answer("mx.acme.test")}, smtp={})
    _service(container, mail__deliverability=checker).recheck_contacts(actor="test")
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        assert contact.delivery_status is ContactDeliveryStatus.DELIVERABLE


def test_a_search_url_citation_is_replaced_only_when_a_page_carries_the_address(
    container: Container,
) -> None:
    search_url = "https://www.google.com/search?q=site%3Alinkedin.com+contact+acme"
    contact_id = _stored_contact(container, "recipient@acme.test", search_url)
    pages = StubPages({"https://acme.test/team": TEAM_PAGE})
    service = _service(container, people__company_pages=pages)

    preview = service.repair_citations(actor="test", apply=False)
    assert len(preview) == 1
    assert preview[0].matched is True
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        assert contact.public_source_url == search_url

    applied = service.repair_citations(actor="test", apply=True)
    assert applied[0].replacement_url == "https://acme.test/team"
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        assert contact.public_source_url == "https://acme.test/team"


def test_an_unevidenced_citation_is_left_alone(container: Container) -> None:
    search_url = "https://www.google.com/search?q=nobody"
    contact_id = _stored_contact(container, "ghost@acme.test", search_url)
    pages = StubPages({"https://acme.test/team": TEAM_PAGE})
    repairs = _service(container, people__company_pages=pages).repair_citations(
        actor="test", apply=True
    )
    assert repairs[0].matched is False
    with container.database.session() as session:
        contact = session.get(PersonContact, contact_id)
        assert contact is not None
        assert contact.public_source_url == search_url
