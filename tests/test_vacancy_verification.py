from __future__ import annotations

import json
from datetime import timedelta
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from sqlalchemy import func, select
from test_application_workflow import URL, verified
from test_opportunities import add_role, assess

from careeros.connectors import vacancy
from careeros.connectors.vacancy import VacancyPageConnector, public_url
from careeros.database.models import ConnectorRun, JobAssessment, VacancyVerification
from careeros.domain.workflow import VacancyStatus


@pytest.fixture
def ready(container, active_profile):
    job_id = add_role(container)
    assess(container, active_profile, job_id)
    with container.database.session() as session:
        container.vacancies.record(
            session,
            job_id=job_id,
            profile_id=active_profile,
            evidence=verified(container),
            actor="Demo Candidate",
            reviewed_by="Demo Candidate",
        )
    return {"job": job_id, "profile": active_profile}


def page(*, title="Researcher", company="Example Capital", deadline=None, url=URL):
    data = {
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {"name": company},
        "url": url,
        "datePosted": "2026-07-29T09:00:00Z",
        "description": "Visa sponsorship available. Python required.",
        "jobLocation": {"address": "London"},
    }
    if deadline:
        data["validThrough"] = deadline
    return '<script type="application/ld+json">' + json.dumps({"@graph": [data]}) + "</script>"


@pytest.mark.parametrize(
    "body,expected",
    [
        (page(), VacancyStatus.OPEN),
        (page(deadline="2026-07-29T09:00:00Z"), VacancyStatus.CLOSED),
        (page(deadline="2026-08-01"), VacancyStatus.UNKNOWN),
        (page(deadline="not-a-date"), VacancyStatus.UNKNOWN),
        (page(title="Other role"), VacancyStatus.UNKNOWN),
        (page(company="Other employer"), VacancyStatus.UNKNOWN),
        (page(url="https://example.test/other"), VacancyStatus.UNKNOWN),
        (page() + page(), VacancyStatus.UNKNOWN),
        ("<h1>Careers</h1><a>Apply</a>", VacancyStatus.UNKNOWN),
        ('<script type="application/ld+json">invalid</script>', VacancyStatus.UNKNOWN),
        ("<script type>broken type attribute</script>", VacancyStatus.UNKNOWN),
    ],
)
def test_metadata_identity_and_uncertainty(container, body, expected):
    result = VacancyPageConnector(container.clock).parse(
        body, url=URL, final_url=URL, title="Researcher", company="Example Capital"
    )
    assert result.status == expected
    assert result.review is None
    if expected == VacancyStatus.OPEN:
        assert result.sponsorship_evidence == ("Visa sponsorship available",)
        assert result.published_at is not None


class Response(BytesIO):
    def __init__(self, body, content_type="text/html"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return URL


@pytest.mark.parametrize("case", ["open", "404", "403", "oversize", "mime"])
def test_http_contract_retains_failures_and_never_equates_404_with_closed(
    container, monkeypatch, case
):
    monkeypatch.setattr(vacancy, "public_url", lambda url: None)

    class Opener:
        def open(self, request, timeout):
            assert timeout == 15
            if request.full_url.endswith("robots.txt"):
                return Response(b"User-agent: *\nAllow: /\n", "text/plain")
            assert request.full_url == URL
            if case in {"404", "403"}:
                raise HTTPError(URL, int(case), "not accessible", {}, None)
            if case == "oversize":
                return Response(b"x" * (vacancy.MAX_BYTES + 1))
            return Response(page().encode(), "application/pdf" if case == "mime" else "text/html")

    monkeypatch.setattr(vacancy, "build_opener", lambda *args: Opener())
    result = VacancyPageConnector(container.clock).check(
        URL, title="Researcher", company="Example Capital"
    )
    assert result.status == (VacancyStatus.OPEN if case == "open" else VacancyStatus.UNREACHABLE)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@example.test", "http://localhost"]
)
def test_private_and_authenticated_urls_are_rejected(monkeypatch, url):
    monkeypatch.setattr(
        vacancy.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ValueError):
        public_url(url)


def test_public_redirect_is_validated(monkeypatch):
    called = []
    monkeypatch.setattr(vacancy, "public_url", lambda url: called.append(url))
    redirected = vacancy.PublicRedirects().redirect_request(
        Request(URL), None, 302, "redirect", {}, "https://example.test/new"
    )
    assert redirected.full_url == "https://example.test/new" and called == [
        "https://example.test/new"
    ]


def test_read_failures_have_persisted_provenance_and_keep_prior_fit(container, ready):
    with container.database.session() as session:
        failure = verified(container, status=VacancyStatus.UNREACHABLE, review=False)
        record = container.vacancies.record(
            session,
            job_id=ready["job"],
            profile_id=ready["profile"],
            evidence=failure,
            actor="test",
            connector_name="public-vacancy-page",
        )
        assert container.vacancies.latest(session, ready["job"], ready["profile"]).id == record.id
        assert session.scalar(select(func.count(ConnectorRun.id))) >= 1
        assert session.scalar(select(JobAssessment.fit_score)) == 0.9
        assert container.audit.verify(session).valid


def test_old_and_future_human_evidence_cannot_replace_current_review(container, ready):
    for offset in (-2, 2):
        with pytest.raises(ValueError), container.database.session() as session:
            container.vacancies.record(
                session,
                job_id=ready["job"],
                profile_id=ready["profile"],
                evidence=verified(container, when=container.clock.now() + timedelta(days=offset)),
                actor="Demo Candidate",
                reviewed_by="Demo Candidate",
            )
    with container.database.session() as session:
        assert session.scalar(select(func.count(VacancyVerification.id))) == 1
