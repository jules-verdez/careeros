"""CareerOS command-line interface."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import inspect, select

from careeros import __version__
from careeros.cli_views import render_opportunities
from careeros.cli_workflow import register_workflow
from careeros.config import Settings, load_environment
from careeros.connectors.ats import AtsJobBoardConnector, JobBoard
from careeros.connectors.base import (
    ConnectorNotConfiguredError,
    JobDiscoveryConnector,
    WebSearchConnector,
)
from careeros.connectors.deliverability import DeliverabilityConnector
from careeros.connectors.github_commits import GitHubCommitEmailConnector
from careeros.connectors.local_json import LocalJsonJobConnector
from careeros.connectors.sponsors import UkSponsorRegisterConnector
from careeros.connectors.team_pages import CompanyPageContactConnector
from careeros.container import Container
from careeros.database.enums import (
    ActionKind,
    ActionStatus,
    ContactEnrichmentReason,
    PreferenceKind,
    SponsorMatchStatus,
    TaskStatus,
)
from careeros.database.models import (
    ActionRequest,
    CandidateProfile,
    Company,
    CompanySponsorshipAssessment,
    ContactEnrichmentObservation,
    Job,
    Person,
    PersonEmployment,
    Task,
)
from careeros.domain.job_review import ReviewEvidence
from careeros.domain.records import DraftMessage, JobDiscoveryQuery
from careeros.migrations import current_revision, upgrade_database
from careeros.services.daily import BriefView
from careeros.services.email_drafts import load_email_draft_packet
from careeros.services.email_sends import load_email_send_packet
from careeros.services.integration_health import IntegrationHealthService, Readiness
from careeros.services.normalization import normalize_text
from careeros.services.opportunities import OpportunityOrder
from careeros.services.outreach import OutreachService

app = typer.Typer(
    name="career",
    help="A human-governed operating system for career discovery and execution.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Manage the candidate profile and preferences.")
person_app = typer.Typer(help="Manage public professional records.")
action_app = typer.Typer(help="Review approval-gated external actions.")
audit_app = typer.Typer(help="Inspect the append-only audit trail.")
db_app = typer.Typer(help="Inspect database migration state.")
task_app = typer.Typer(help="Inspect and update the deterministic work queue.")
sponsor_app = typer.Typer(help="Match companies against the official UKVI sponsor register.")
evidence_app = typer.Typer(help="Run bounded source discovery without persisting snippets.")
contact_app = typer.Typer(help="Find and judge public addresses using free sources.")
app.add_typer(profile_app, name="profile")
app.add_typer(person_app, name="person")
app.add_typer(action_app, name="action")
app.add_typer(audit_app, name="audit")
app.add_typer(db_app, name="db")
app.add_typer(task_app, name="task")
app.add_typer(sponsor_app, name="sponsor")
app.add_typer(evidence_app, name="evidence")
app.add_typer(contact_app, name="contact")
register_workflow(app, profile_app)

console = Console()


@dataclass
class CliState:
    settings: Settings
    _container: Container | None = None

    @property
    def container(self) -> Container:
        if self._container is None:
            self._container = Container.build(self.settings)
        return self._container

    def close(self) -> None:
        if self._container is not None:
            self._container.database.dispose()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"CareerOS {__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    database_url: Annotated[
        str | None,
        typer.Option(
            "--database-url",
            envvar="CAREEROS_DATABASE_URL",
            help="SQLAlchemy database URL.",
        ),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    del version
    load_environment()
    settings = Settings(database_url=database_url) if database_url else Settings()
    cli_state = CliState(settings=settings)
    ctx.obj = cli_state
    ctx.call_on_close(cli_state.close)


def state(ctx: typer.Context) -> CliState:
    return ctx.ensure_object(CliState)


def require_initialized(container: Container) -> None:
    if not inspect(container.database.engine).has_table("alembic_version"):
        console.print("[red]CareerOS is not initialized. Run `career init` first.[/red]")
        raise typer.Exit(code=2)


def active_profile(container: Container) -> CandidateProfile:
    with container.database.session() as session:
        profile = session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.created_at)
            .limit(1)
        )
        if profile is None:
            raise RuntimeError("no active candidate profile; run `career init`")
        return profile


@app.command("init")
def initialize(
    ctx: typer.Context,
    email: Annotated[
        str | None,
        typer.Option(help="Candidate sending address; may also be added later."),
    ] = None,
) -> None:
    """Apply migrations and create the active candidate profile."""

    cli = state(ctx)
    upgrade_database(cli.settings.database_url)
    container = cli.container
    with container.database.session() as session:
        profile = container.profiles.ensure_profile(
            session,
            name=cli.settings.candidate_name,
            email=email,
            actor="career.init",
        )
    console.print(
        f"[green]CareerOS initialized[/green] for {profile.name} "
        f"(profile {profile.id}, {cli.settings.database_url})."
    )


@profile_app.command("show")
def profile_show(ctx: typer.Context) -> None:
    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    table = Table(title="Active profile")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Email")
    table.add_row(profile.id, profile.name, profile.primary_email or "not configured")
    console.print(table)


@profile_app.command("preference")
def profile_preference(
    ctx: typer.Context,
    kind: Annotated[PreferenceKind, typer.Option(case_sensitive=False)],
    value: Annotated[str, typer.Argument(help="Preferred value.")],
    weight: Annotated[float, typer.Option(min=0)] = 1.0,
    required: Annotated[bool, typer.Option("--required/--optional")] = False,
) -> None:
    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    with container.database.session() as session:
        preference = container.profiles.add_preference(
            session,
            profile_id=profile.id,
            kind=kind,
            value=value,
            weight=weight,
            is_required=required,
            actor="career.profile",
        )
    console.print(f"[green]Saved[/green] {preference.kind.value}: {preference.value}")


@app.command()
def discover(
    ctx: typer.Context,
    connector_name: Annotated[
        str,
        typer.Option(
            "--connector",
            help="local-json, greenhouse, lever, ashby, or smartrecruiters.",
        ),
    ] = "local-json",
    from_json: Annotated[
        Path | None,
        typer.Option(
            "--from-json",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Import real job observations exported as JSON. Requires --connector local-json.",
        ),
    ] = None,
    company: Annotated[
        str | None,
        typer.Option("--company", help="The company's slug on the chosen job board."),
    ] = None,
    company_name: Annotated[
        str | None,
        typer.Option("--company-name", help="Canonical firm name, when it differs from the slug."),
    ] = None,
    company_domain: Annotated[
        str | None,
        typer.Option("--company-domain", help="Mail domain, so outreach can join on it later."),
    ] = None,
    since_hours: Annotated[
        int | None,
        typer.Option("--since-hours", min=1, max=8_760, help="Drop postings older than this."),
    ] = None,
    role: Annotated[
        list[str] | None,
        typer.Option("--role", help="Match job titles containing this text; repeatable."),
    ] = None,
    region: Annotated[
        list[str] | None,
        typer.Option("--region", help="Match locations containing this text; repeatable."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 100,
) -> None:
    """Discover and deduplicate jobs through an explicit connector."""

    container = state(ctx).container
    require_initialized(container)
    connector = _job_connector(connector_name, from_json, company, company_name, company_domain)
    query = JobDiscoveryQuery(
        roles=tuple(role or ()),
        regions=tuple(region or ()),
        posted_since=(
            datetime.now(UTC) - timedelta(hours=since_hours) if since_hours is not None else None
        ),
        limit=limit,
    )
    summary = container.discovery_agent.run(connector, query)
    console.print("[green]Discovery complete[/green]")
    for key, value in summary.as_dict().items():
        console.print(f"  {key.replace('_', ' ')}: {value}")


def _job_connector(
    connector_name: str,
    from_json: Path | None,
    company: str | None,
    company_name: str | None,
    company_domain: str | None,
) -> JobDiscoveryConnector:
    """Pick the connector the operator named, refusing an incompletely specified run.

    Each connector needs a different mandatory argument, and getting it wrong produces an
    empty result rather than an error, so the mismatch is rejected here instead.
    """
    if connector_name == "local-json":
        if from_json is None:
            raise typer.BadParameter("--connector local-json requires --from-json.")
        return LocalJsonJobConnector(from_json)

    try:
        board = JobBoard(connector_name)
    except ValueError as error:
        options = ", ".join(("local-json", *(item.value for item in JobBoard)))
        raise typer.BadParameter(
            f"Unknown connector {connector_name!r}. Use one of: {options}."
        ) from error

    if not company:
        raise typer.BadParameter(f"--connector {connector_name} requires --company <board slug>.")
    return AtsJobBoardConnector(
        board,
        company,
        company_name=company_name,
        company_domain=company_domain,
    )


@app.command("jobs")
def list_jobs(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
    sponsor_only: Annotated[
        bool,
        typer.Option("--sponsor-only", help="Show exact licensed-sponsor matches only."),
    ] = False,
) -> None:
    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    with container.database.session() as session:
        query = (
            select(Job, Company, CompanySponsorshipAssessment)
            .join(Company, Company.id == Job.company_id)
            .outerjoin(
                CompanySponsorshipAssessment,
                CompanySponsorshipAssessment.company_id == Company.id,
            )
            .order_by(Job.first_seen_at.desc())
            .limit(limit)
        )
        if sponsor_only:
            query = query.where(CompanySponsorshipAssessment.status == SponsorMatchStatus.MATCHED)
        rows = session.execute(query).all()
        latest = {
            item.job.id: item.assessment
            for item in container.assessments.latest(session, profile_id=profile.id)
        }
        table = Table(title="Jobs")
        table.add_column("ID", no_wrap=True)
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Location")
        table.add_column("Status")
        table.add_column("Sponsor")
        table.add_column("Fit")
        for job, company, sponsorship in rows:
            assessment = latest.get(job.id)
            table.add_row(
                job.id,
                company.canonical_name,
                job.title,
                job.location or "—",
                job.status.value,
                sponsorship.status.value if sponsorship else "unchecked",
                str(container.assessments.profile_fit_score(assessment))
                if assessment
                else "unscored",
            )
        queue = container.opportunities.queue(session, profile_id=profile.id)
    console.print(table)
    render_opportunities(console, queue, mandatory_only=True)


@app.command()
def opportunities(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
    order: Annotated[OpportunityOrder, typer.Option("--sort")] = OpportunityOrder.RECENT,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit complete structured evidence.")
    ] = False,
) -> None:
    """Review saved roles once each, with an uncapped mandatory high-fit section."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    with container.database.session() as session:
        queue = container.opportunities.queue(
            session, profile_id=profile.id, limit=limit, order=order
        )
    if as_json:
        typer.echo(queue.model_dump_json(indent=2))
    else:
        render_opportunities(console, queue)


@sponsor_app.command("sync")
def sponsor_sync(
    ctx: typer.Context,
    company: Annotated[
        str | None,
        typer.Option("--company", help="Restrict matching to one stored canonical company name."),
    ] = None,
) -> None:
    """Download the current official register and persist conservative matches."""

    container = state(ctx).container
    require_initialized(container)
    connector = cast(
        UkSponsorRegisterConnector,
        container.connectors.require("sponsors.ukvi"),
    )
    run_id = container.ledger.start_connector(
        name=connector.name,
        operation="sync_sponsor_register",
        request_json={"company": company},
    )
    try:
        snapshot = connector.fetch(as_of=container.clock.now())
    except Exception as error:
        container.ledger.finish_connector(run_id, error=error)
        raise
    container.ledger.finish_connector(
        run_id,
        response_summary_json={
            "entries": len(snapshot.entries),
            "source_sha256": snapshot.source_sha256,
        },
    )
    summary = container.sponsorships.sync(
        snapshot,
        actor="career.sponsor",
        company_name=company,
    )
    console.print("[green]Sponsor register synchronized[/green]")
    for key, value in summary.as_dict().items():
        console.print(f"  {key.replace('_', ' ')}: {value}")
    if summary.probable_alias or summary.ambiguous:
        console.print(
            "[yellow]Probable and ambiguous names remain review-only; add an explicit alias "
            "before treating them as licensed.[/yellow]"
        )


@sponsor_app.command("alias")
def sponsor_alias(
    ctx: typer.Context,
    company_id: Annotated[str, typer.Argument(help="Stored company ID.")],
    licensed_name: Annotated[str, typer.Argument(help="Exact organisation name in the register.")],
) -> None:
    """Record a human-reviewed licensed-name alias for a company."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        alias = container.sponsorships.add_alias(
            session,
            company_id=company_id,
            licensed_name=licensed_name,
            actor="career.sponsor",
        )
    console.print(f"[green]Saved sponsor alias[/green] {alias.alias}")


@sponsor_app.command("list")
def sponsor_list(
    ctx: typer.Context,
    status: Annotated[
        SponsorMatchStatus | None,
        typer.Option(case_sensitive=False, help="Filter by match status."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """Display persisted sponsor assessments and their provenance."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        query = (
            select(Company, CompanySponsorshipAssessment)
            .join(
                CompanySponsorshipAssessment,
                CompanySponsorshipAssessment.company_id == Company.id,
            )
            .order_by(Company.canonical_name)
            .limit(limit)
        )
        if status is not None:
            query = query.where(CompanySponsorshipAssessment.status == status)
        rows = session.execute(query).all()
    table = Table(title="UKVI sponsor assessments")
    table.add_column("Company")
    table.add_column("Status")
    table.add_column("Licensed organisation")
    table.add_column("Basis")
    table.add_column("Score")
    for company, assessment in rows:
        table.add_row(
            company.canonical_name,
            assessment.status.value,
            assessment.matched_organisation or "—",
            assessment.match_basis,
            f"{assessment.match_score:.2f}",
        )
    console.print(table)


@app.command()
def assess(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument()],
    fit: Annotated[
        float, typer.Option(min=0, max=1, help="Professional fit before eligibility penalties.")
    ],
    confidence: Annotated[float, typer.Option(min=0, max=1)],
    competition: Annotated[float, typer.Option(min=0, max=1)],
    why: Annotated[str, typer.Option(help="Evidence-based assessment rationale.")],
    review_file: Annotated[
        Path | None,
        typer.Option(
            "--review-file",
            exists=True,
            dir_okay=False,
            readable=True,
            help="JSON containing vacancy-specific eligibility evidence.",
        ),
    ] = None,
) -> None:
    """Persist an explicit, versioned job assessment."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    review_evidence = None
    if review_file is not None:
        try:
            review_evidence = ReviewEvidence.model_validate_json(
                review_file.read_text(encoding="utf-8")
            )
        except (ValueError, OSError) as error:
            raise typer.BadParameter(str(error), param_hint="--review-file") from error
    with container.database.session() as session:
        assessment = container.assessments.assess(
            session,
            job_id=job_id,
            profile_id=profile.id,
            fit_score=fit,
            confidence=confidence,
            competition_score=competition,
            explanation={"rationale": why, "epistemic_status": "human"},
            actor="career.assess",
            review=review_evidence,
        )
    console.print(f"[green]Assessment saved[/green] ({assessment.id}).")


@app.command()
def shortlist(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument()],
) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        job = container.assessments.shortlist(
            session,
            job_id=job_id,
            actor="career.shortlist",
        )
    console.print(f"[green]Shortlisted[/green] {job.title} ({job.id}).")


@app.command()
def research(
    ctx: typer.Context,
    company: Annotated[str, typer.Argument(help="Canonical company name.")],
    markdown: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True),
    ],
    structured_json: Annotated[
        Path,
        typer.Option(
            "--json",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Structured research JSON.",
        ),
    ],
    as_of: Annotated[str, typer.Option(help="Snapshot date in YYYY-MM-DD format.")],
    confidence: Annotated[float, typer.Option(min=0, max=1)],
) -> None:
    """Import one immutable markdown+JSON company research snapshot."""

    container = state(ctx).container
    require_initialized(container)
    try:
        snapshot_date = date.fromisoformat(as_of)
    except ValueError as error:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--as-of") from error
    structured = json.loads(structured_json.read_text(encoding="utf-8"))
    if not isinstance(structured, dict):
        raise typer.BadParameter("research JSON must be an object", param_hint="--json")
    with container.database.session() as session:
        report = container.research.import_snapshot(
            session,
            company_name=company,
            as_of_date=snapshot_date,
            markdown=markdown.read_text(encoding="utf-8"),
            structured=structured,
            confidence=confidence,
            actor="career.research",
        )
    console.print(f"[green]Research snapshot imported[/green] ({report.id}).")


@person_app.command("add")
def person_add(
    ctx: typer.Context,
    company: Annotated[str, typer.Option(help="Canonical company name.")],
    name: Annotated[str, typer.Option()],
    title: Annotated[str, typer.Option()],
    influence: Annotated[float, typer.Option(min=0, max=1)],
    confidence: Annotated[float, typer.Option(min=0, max=1)],
    linkedin: Annotated[str | None, typer.Option()] = None,
    email: Annotated[str | None, typer.Option()] = None,
    email_source_url: Annotated[
        str | None,
        typer.Option(help="Required public page citation when --email is supplied."),
    ] = None,
) -> None:
    """Store a public decision-maker record with provenance."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        company_row = session.scalar(
            select(Company).where(Company.normalized_name == normalize_text(company))
        )
        if company_row is None:
            raise typer.BadParameter(f"company not found: {company}", param_hint="--company")
        person = container.people.add_public_person(
            session,
            company_id=company_row.id,
            full_name=name,
            title=title,
            influence_probability=influence,
            confidence=confidence,
            linkedin_url=linkedin,
            email=email,
            email_source_url=email_source_url,
            actor="career.person",
        )
    console.print(f"[green]Public person record saved[/green] ({person.id}).")


@person_app.command("list")
def person_list(ctx: typer.Context) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        rows = session.execute(
            select(Person, PersonEmployment, Company)
            .join(PersonEmployment, PersonEmployment.person_id == Person.id)
            .join(Company, Company.id == PersonEmployment.company_id)
            .where(PersonEmployment.is_current.is_(True))
            .order_by(Company.canonical_name, Person.full_name)
        ).all()
        table = Table(title="Decision-makers")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Company")
        table.add_column("Role")
        table.add_column("Influence")
        table.add_column("Confidence")
        for person, employment, company in rows:
            table.add_row(
                person.id,
                person.full_name,
                company.canonical_name,
                employment.title,
                f"{employment.hiring_influence_probability or 0:.2f}",
                f"{employment.confidence:.2f}",
            )
    console.print(table)


@person_app.command("enrich")
def person_enrich(
    ctx: typer.Context,
    person_id: Annotated[str, typer.Argument(help="Known person ID.")],
    reason: Annotated[
        ContactEnrichmentReason,
        typer.Option(case_sensitive=False, help="Why Hunter could not resolve this person."),
    ],
    provider: Annotated[
        str,
        typer.Option(help="Fallback provider; currently prospeo."),
    ] = "prospeo",
    spend_one_credit: Annotated[
        bool,
        typer.Option(
            "--spend-one-credit",
            help="Authorize at most one provider credit for this exact person.",
        ),
    ] = False,
) -> None:
    """Run one paid-capable fallback lookup without promoting it to a sendable contact."""

    if provider.casefold() != "prospeo":
        raise typer.BadParameter("the only configured fallback provider is prospeo")
    if not spend_one_credit:
        raise typer.BadParameter(
            "Prospeo may spend one credit; add --spend-one-credit to authorize this lookup."
        )
    container = state(ctx).container
    require_initialized(container)
    run_id = container.ledger.start_connector(
        name="prospeo",
        operation="enrich_person_fallback",
        request_json={"person_id": person_id, "fallback_reason": reason.value},
    )
    try:
        outcome = container.contact_enrichment.enrich(
            person_id,
            capability="contact_enrichment.prospeo",
            reason=reason,
            allow_credit_spend=True,
            actor="career.person.enrich",
        )
    except Exception as error:
        container.ledger.finish_connector(run_id, error=error)
        raise
    container.ledger.finish_connector(
        run_id,
        response_summary_json={
            "provider_status": outcome.result.email_status,
            "is_verified": outcome.result.is_verified,
            "credits_spent": outcome.result.credits_spent,
            "observation_id": outcome.observation_id,
        },
    )
    console.print("[green]Fallback observation recorded[/green]")
    console.print(f"  observation: {outcome.observation_id}")
    console.print(f"  email: {outcome.result.email_address or 'not found'}")
    console.print(f"  provider status: {outcome.result.email_status}")
    console.print(f"  credits spent: {outcome.result.credits_spent}")
    console.print(
        "[yellow]This vendor observation was not promoted into the public-email send path. "
        "Add a genuine public source before outreach.[/yellow]"
    )


@person_app.command("enrichments")
def person_enrichments(
    ctx: typer.Context,
    person_id: Annotated[str | None, typer.Option(help="Restrict to one person ID.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    """List provider observations separately from sendable public contacts."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        query = (
            select(ContactEnrichmentObservation, Person)
            .join(Person, Person.id == ContactEnrichmentObservation.person_id)
            .order_by(ContactEnrichmentObservation.observed_at.desc())
            .limit(limit)
        )
        if person_id:
            query = query.where(ContactEnrichmentObservation.person_id == person_id)
        rows = session.execute(query).all()
    table = Table(title="Contact-enrichment observations")
    table.add_column("ID")
    table.add_column("Person")
    table.add_column("Provider")
    table.add_column("Reason")
    table.add_column("Status")
    table.add_column("Email")
    table.add_column("Credits")
    for observation, person in rows:
        table.add_row(
            observation.id,
            person.full_name,
            observation.provider,
            observation.fallback_reason.value,
            observation.provider_status,
            observation.email_address or "—",
            str(observation.credits_spent),
        )
    console.print(table)


@evidence_app.command("search")
def evidence_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Source-discovery query.")],
    count: Annotated[int, typer.Option(min=1, max=20)] = 10,
    country: Annotated[str, typer.Option(help="Two-letter result country.")] = "GB",
    spend_one_request: Annotated[
        bool,
        typer.Option(
            "--spend-one-request",
            help="Authorize exactly one Brave Search API request.",
        ),
    ] = False,
) -> None:
    """Search for source URLs transiently; snippets are not written to CareerOS."""

    if not spend_one_request:
        raise typer.BadParameter(
            "Brave Search may be billable; add --spend-one-request to authorize one request."
        )
    if len(country) != 2 or not country.isalpha():
        raise typer.BadParameter("country must be a two-letter code", param_hint="--country")
    container = state(ctx).container
    require_initialized(container)
    connector = cast(WebSearchConnector, container.connectors.require("search.brave"))
    run_id = container.ledger.start_connector(
        name=connector.name,
        operation="evidence_web_search",
        request_json={"query": query, "count": count, "country": country.upper()},
    )
    try:
        results = connector.search(
            query,
            count=count,
            country=country,
            allow_request_spend=True,
        )
    except Exception as error:
        container.ledger.finish_connector(run_id, error=error)
        raise
    container.ledger.finish_connector(
        run_id,
        response_summary_json={"results": len(results), "requests_spent": 1},
    )
    table = Table(title="Transient evidence search")
    table.add_column("Title")
    table.add_column("URL")
    table.add_column("Age")
    for result in results:
        table.add_row(result.title, str(result.url), result.page_age or "—")
    console.print(table)
    console.print("No result snippets were persisted.")


@app.command()
def outreach(
    ctx: typer.Context,
    person_id: Annotated[str, typer.Argument()],
    subject: Annotated[str, typer.Option()],
    body: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True),
    ],
    why: Annotated[
        list[str] | None,
        typer.Option(help="One rationale per paragraph; repeat this option."),
    ] = None,
    evidence: Annotated[
        list[str] | None,
        typer.Option(help="Supporting document ID; repeat this option."),
    ] = None,
    connector: Annotated[str, typer.Option()] = "gmail",
    reply_to: Annotated[
        str | None,
        typer.Option(
            "--reply-to",
            help="CareerOS email ID of the sent message this follows up; keeps the Gmail thread.",
        ),
    ] = None,
) -> None:
    """Stage an evidence-bound email and a send approval; never send it."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    message = DraftMessage(
        subject=subject,
        body=body.read_text(encoding="utf-8"),
        paragraph_rationale=tuple(why or []),
        evidence_document_ids=tuple(evidence or []),
    )
    service = OutreachService(container.approvals, container.audit, container.contact_policy)
    with container.database.session() as session:
        email, action_id = service.stage_email(
            session,
            profile_id=profile.id,
            person_id=person_id,
            message=message,
            connector_name=connector,
            actor="career.outreach",
            in_reply_to_email_id=reply_to,
        )
    console.print(f"[green]Local draft staged[/green] ({email.id}).")
    console.print(f"Pending send approval: [bold]{action_id}[/bold]. Nothing was sent.")


@action_app.command("list")
def action_list(
    ctx: typer.Context,
    status: Annotated[ActionStatus | None, typer.Option(case_sensitive=False)] = None,
) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        query = select(ActionRequest).order_by(ActionRequest.requested_at.desc())
        if status:
            query = query.where(ActionRequest.status == status)
        actions = session.scalars(query).all()
        table = Table(title="Action requests")
        table.add_column("ID")
        table.add_column("Kind")
        table.add_column("Status")
        table.add_column("Summary")
        table.add_column("Requested")
        for action in actions:
            table.add_row(
                action.id,
                action.kind.value,
                action.status.value,
                action.summary,
                action.requested_at.isoformat(timespec="minutes"),
            )
    console.print(table)


@action_app.command("show")
def action_show(
    ctx: typer.Context,
    action_id: Annotated[str, typer.Argument()],
) -> None:
    """Show the exact immutable packet before approval."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        action = session.get(ActionRequest, action_id)
        if action is None:
            raise LookupError(f"action request not found: {action_id}")
        console.print(f"Action: {action.id}")
        console.print(f"Kind: {action.kind.value}")
        console.print(f"Status: {action.status.value}")
        console.print(f"Payload SHA-256: {action.payload_sha256}")
        if action.kind not in {ActionKind.CREATE_EMAIL_DRAFT, ActionKind.SEND_EMAIL}:
            console.print_json(data=action.payload_json)
            return
        email_id = action.payload_json.get("email_id")
        if not isinstance(email_id, str):
            raise RuntimeError("email action has no email_id")
        if action.kind is ActionKind.SEND_EMAIL:
            rfc822_message_id = action.payload_json.get("rfc822_message_id")
            if not isinstance(rfc822_message_id, str) or not rfc822_message_id:
                console.print_json(data=action.payload_json)
                return
            external_thread_id = action.payload_json.get("external_thread_id")
            if not isinstance(external_thread_id, str) or not external_thread_id:
                console.print_json(data=action.payload_json)
                return
            send_packet = load_email_send_packet(
                session,
                email_id,
                rfc822_message_id=rfc822_message_id,
                external_thread_id=external_thread_id,
            )
            packet = send_packet.email
            console.print(f"Gmail draft: {send_packet.external_draft_id}")
            console.print("Effect: SEND THIS EMAIL")
        else:
            packet = load_email_draft_packet(session, email_id)
            console.print("Effect: CREATE A GMAIL DRAFT ONLY")
        console.print(f"From: {packet.sender}")
        console.print(f"To: {packet.recipient}")
        console.print(f"Subject: {packet.subject}")
        evidence = packet.approval_payload["evidence"]
        if evidence:
            console.print("Evidence:")
            for item in evidence:
                console.print(f"- {item['document_id']}: {item['source_url'] or 'local document'}")
        console.print("Body:")
        console.print(packet.body, markup=False)


@action_app.command("request-send")
def action_request_send(
    ctx: typer.Context,
    email_id: Annotated[str, typer.Argument()],
    actor: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Stage a separate send approval for an existing approved Gmail draft."""

    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        action = container.email_sends.request(
            session, email_id, actor=actor or active_profile(container).name
        )
    console.print(
        f"[yellow]Send approval required[/yellow]: [bold]{action.id}[/bold]. Nothing was sent."
    )


@action_app.command("approve")
def action_approve(
    ctx: typer.Context,
    action_id: Annotated[str, typer.Argument()],
    reviewer: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Approve the immutable payload; execution remains a separate operation."""

    container = state(ctx).container
    require_initialized(container)
    try:
        with container.database.session() as session:
            action = container.approvals.approve(
                session, action_id, reviewer=reviewer or active_profile(container).name
            )
    except (ValueError, LookupError, RuntimeError) as error:
        console.print(str(error), markup=False)
        raise typer.Exit(code=2) from error
    console.print(
        f"[green]Approved[/green] {action.id}. No external action was executed by this command."
    )


@action_app.command("execute")
def action_execute(
    ctx: typer.Context,
    action_id: Annotated[str, typer.Argument()],
    actor: Annotated[str | None, typer.Option()] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Retry an interrupted or failed execution with the same idempotency key.",
        ),
    ] = False,
) -> None:
    """Execute one approved Gmail draft-creation or email-send action."""

    container = state(ctx).container
    require_initialized(container)
    actor = actor or active_profile(container).name
    try:
        with container.database.session() as session:
            action = session.get(ActionRequest, action_id)
            if action is None:
                raise LookupError(f"action request not found: {action_id}")
            kind = action.kind
        if kind is ActionKind.CREATE_EMAIL_DRAFT:
            result = container.email_drafts.execute(action_id, actor=actor, resume=resume)
            if result.already_executed:
                console.print(
                    f"[green]Gmail draft already exists[/green] ({result.external_draft_id}). "
                    "Nothing was sent."
                )
                return
            console.print(
                f"[green]Gmail draft created[/green] ({result.external_draft_id}). "
                "Nothing was sent."
            )
            return
        if kind is ActionKind.SEND_EMAIL:
            send_result = container.email_sends.execute(action_id, actor=actor, resume=resume)
            qualifier = "already sent" if send_result.already_executed else "sent"
            console.print(f"[green]Email {qualifier}[/green] ({send_result.external_message_id}).")
            return
        raise RuntimeError(f"action {action_id} has no executable handler: {kind.value}")
    except (LookupError, RuntimeError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from error


@action_app.command("reject")
def action_reject(
    ctx: typer.Context,
    action_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option()],
    reviewer: Annotated[str | None, typer.Option()] = None,
) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        action = container.approvals.reject(
            session,
            action_id,
            reviewer=reviewer or active_profile(container).name,
            reason=reason,
        )
    console.print(f"[yellow]Rejected[/yellow] {action.id}.")


@app.command()
def review(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=25)] = 10,
) -> None:
    """Run the morning review and reconcile the next-action queue."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    result = container.daily_agent.run(profile_id=profile.id, limit=limit)
    render_brief(result.candidate_name, result.view)


@app.command()
def dashboard(ctx: typer.Context) -> None:
    """Show the latest persisted daily dashboard."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    with container.database.session() as session:
        view = container.briefs.latest(session, profile_id=profile.id)
    if view is None:
        result = container.daily_agent.run(profile_id=profile.id)
        render_brief(result.candidate_name, result.view)
    else:
        render_brief(profile.name, view)


@app.command()
def followup(ctx: typer.Context) -> None:
    """Reconcile and display follow-ups due."""

    container = state(ctx).container
    require_initialized(container)
    profile = active_profile(container)
    with container.database.session() as session:
        container.planner.reconcile(
            session,
            profile_id=profile.id,
            actor="followup-agent",
        )
        tasks = session.scalars(
            select(Task)
            .where(
                Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]),
                Task.title.like("Follow up:%"),
            )
            .order_by(Task.priority.desc(), Task.due_at)
        ).all()
    render_tasks(tasks, title="Follow-ups due")


@app.command()
def inbox(
    ctx: typer.Context,
    since_hours: Annotated[
        int,
        typer.Option("--since-hours", min=1, max=8_760, help="Reconcile this recent window."),
    ] = 168,
) -> None:
    """Reconcile replies and delivery failures without opening unrelated messages."""

    container = state(ctx).container
    require_initialized(container)
    try:
        summary = container.inbox.reconcile(
            after=container.clock.now() - timedelta(hours=since_hours),
            actor="career.inbox",
        )
    except ConnectorNotConfiguredError as error:
        console.print(f"[yellow]{error}[/yellow]")
        console.print(
            "Install/configure the Google Workspace connector before inbox access. "
            "No mailbox data was read."
        )
        raise typer.Exit(code=2) from error
    console.print("[green]Inbox reconciliation complete[/green]")
    for key, value in summary.as_dict().items():
        console.print(f"  {key.replace('_', ' ')}: {value}")


@app.command()
def doctor(
    live: Annotated[
        bool,
        typer.Option(
            "--live/--local-only",
            help="Make a read-only Gmail profile request after validating local credentials.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Report whether external tools are installed, authenticated, and usable."""

    results = IntegrationHealthService().check_all(live=live)
    if json_output:
        console.print_json(data=[result.as_dict() for result in results])
    else:
        table = Table(title="Integration readiness")
        table.add_column("Integration")
        table.add_column("Status")
        table.add_column("Identity")
        table.add_column("Detail")
        colors = {
            Readiness.READY: "green",
            Readiness.PARTIAL: "yellow",
            Readiness.MISSING: "red",
            Readiness.ERROR: "red",
        }
        for result in results:
            color = colors[result.readiness]
            table.add_row(
                result.integration,
                f"[{color}]{result.readiness.value}[/{color}]",
                result.identity or "—",
                result.detail,
            )
        console.print(table)
    if any(result.readiness is Readiness.ERROR for result in results):
        raise typer.Exit(code=1)


@task_app.command("list")
def task_list(ctx: typer.Context) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        tasks = session.scalars(
            select(Task)
            .where(Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]))
            .order_by(Task.priority.desc(), Task.due_at)
        ).all()
    render_tasks(tasks)


@task_app.command("done")
def task_done(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument()],
) -> None:
    container = state(ctx).container
    require_initialized(container)
    try:
        with container.database.session() as session:
            task = container.tasks.complete(session, task_id, actor="career.task")
    except LookupError as error:
        raise typer.BadParameter(str(error)) from error
    console.print(f"[green]Completed[/green] {task.title}")


@audit_app.command("verify")
def audit_verify(ctx: typer.Context) -> None:
    container = state(ctx).container
    require_initialized(container)
    with container.database.session() as session:
        result = container.audit.verify(session)
    if not result.valid:
        console.print(
            f"[red]Audit chain invalid at sequence {result.first_invalid_sequence}.[/red]"
        )
        raise typer.Exit(code=1)
    console.print(f"[green]Audit chain valid[/green] ({result.events_checked} events).")


@db_app.command("current")
def db_current(ctx: typer.Context) -> None:
    current_revision(state(ctx).settings.database_url)


def render_brief(candidate_name: str, view: BriefView) -> None:
    summary = view.brief.summary_json
    console.print(f"\n[bold]Good morning {candidate_name}.[/bold]\n")
    table = Table(
        title=f"Saved brief for {view.brief.brief_date} — revision {view.brief.revision}",
        show_header=False,
    )
    console.print(f"Generated: {view.brief.generated_at.isoformat(timespec='minutes')}")
    labels = {
        "upcoming_interviews": "Interviews in next 7 days",
        "new_jobs": "New jobs today",
        "companies_hiring": "Companies hiring",
        "followups_due": "Follow-ups due",
        "recruiter_replies": "Recruiter replies today",
        "open_applications": "Open applications",
        "pending_approvals": "Pending approvals",
        "draft_emails": "Draft emails",
    }
    for key, label in labels.items():
        table.add_row(label, str(summary.get(key, 0)))
    console.print(table)
    recommendations = Table(title="Recommended actions")
    recommendations.add_column("#")
    recommendations.add_column("Action")
    recommendations.add_column("Why")
    recommendations.add_column("Minutes", justify="right")
    for item in view.items:
        recommendations.add_row(
            str(item.rank),
            item.recommendation,
            item.rationale,
            str(item.estimated_minutes),
        )
    console.print(recommendations)
    console.print(f"Estimated operator time: [bold]{view.brief.estimated_minutes} minutes[/bold]")
    if view.items:
        console.print(
            "Highest expected-value proxy: "
            f"[bold]{view.items[0].recommendation}[/bold] "
            f"({view.items[0].expected_roi:.2f})"
        )
    render_opportunities(console, view.opportunity_queue, mandatory_only=True)


def render_tasks(tasks: Sequence[Task], *, title: str = "Work queue") -> None:
    table = Table(title=title)
    table.add_column("ID")
    table.add_column("Priority", justify="right")
    table.add_column("Task")
    table.add_column("Due")
    table.add_column("Minutes", justify="right")
    for task in tasks:
        table.add_row(
            task.id,
            str(task.priority),
            task.title,
            task.due_at.isoformat(timespec="minutes") if task.due_at else "—",
            str(task.estimated_minutes or 0),
        )
    console.print(table)


if __name__ == "__main__":
    app()


STATUS_STYLE = {
    "deliverable": "bold green",
    "invalid": "bold red",
    "risky": "yellow",
    "unknown": "dim",
}


@contact_app.command("check")
def contact_check(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="Address to judge.")],
    smtp: Annotated[
        bool,
        typer.Option(
            "--smtp",
            help=(
                "Also ask the mail server whether the mailbox exists. This opens a "
                "connection to a third party and is blocked on most networks."
            ),
        ),
    ] = False,
) -> None:
    """Judge one address for free: DNS always, SMTP only when authorized."""

    container = state(ctx).container
    require_initialized(container)
    checker = cast(DeliverabilityConnector, container.connectors.require("mail.deliverability"))
    verdict = checker.check(email, allow_smtp_probe=smtp)
    style = STATUS_STYLE.get(verdict.status.value, "dim")
    table = Table(title=f"Deliverability: {verdict.email}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", f"[{style}]{verdict.status.value}[/]")
    table.add_row("Method", verdict.method)
    table.add_row("Mail exchanger", "yes" if verdict.has_mx else "no")
    table.add_row(
        "Catch-all",
        "not asked" if verdict.is_catch_all is None else ("yes" if verdict.is_catch_all else "no"),
    )
    table.add_row("Detail", verdict.detail)
    console.print(table)
    console.print("[dim]No vendor credit was spent.[/dim]")


@contact_app.command("recheck")
def contact_recheck(
    ctx: typer.Context,
    smtp: Annotated[bool, typer.Option("--smtp", help="Also probe each mailbox.")] = False,
    limit: Annotated[int | None, typer.Option(min=1, help="Check at most this many.")] = None,
    only_unknown: Annotated[
        bool,
        typer.Option("--only-unknown", help="Skip contacts whose status is already decided."),
    ] = False,
) -> None:
    """Re-judge every stored address against DNS before any credit is spent."""

    container = state(ctx).container
    require_initialized(container)
    results = container.deliverability.recheck_contacts(
        actor="career.contact.recheck",
        allow_smtp_probe=smtp,
        limit=limit,
        only_unknown=only_unknown,
    )
    table = Table(title="Stored contacts rechecked")
    table.add_column("Person")
    table.add_column("Address")
    table.add_column("Was")
    table.add_column("Now")
    table.add_column("Why")
    for check in results:
        style = STATUS_STYLE.get(check.verdict.status.value, "dim")
        table.add_row(
            check.person_name,
            check.email,
            check.previous_status.value,
            f"[{style}]{check.verdict.status.value}[/]",
            check.verdict.detail[:60],
        )
    console.print(table)
    changed = sum(1 for check in results if check.changed)
    console.print(f"{len(results)} checked, {changed} updated, no vendor credit spent.")


@contact_app.command("citations")
def contact_citations(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the replacement citations found."),
    ] = False,
) -> None:
    """Replace search-result citations with the company page that prints the address."""

    container = state(ctx).container
    require_initialized(container)
    repairs = container.deliverability.repair_citations(
        actor="career.contact.citations",
        apply=apply,
    )
    if not repairs:
        console.print("[green]Every stored address cites a page that carries it.[/green]")
        return
    table = Table(title="Weak citations")
    table.add_column("Address")
    table.add_column("Stored source")
    table.add_column("Replacement")
    for repair in repairs:
        table.add_row(
            repair.email,
            (repair.previous_url or "none")[:48],
            f"[green]{repair.replacement_url}[/green]"
            if repair.matched
            else "[red]none found[/red]",
        )
    console.print(table)
    if not apply:
        console.print("Nothing written. Re-run with --apply to persist the replacements.")


@contact_app.command("github")
def contact_github(
    ctx: typer.Context,
    org: Annotated[str, typer.Argument(help="GitHub organisation slug.")],
    domain: Annotated[
        str | None,
        typer.Option(help="Keep only addresses at this mail domain; strongly recommended."),
    ] = None,
    repos: Annotated[int, typer.Option(min=1, max=100, help="Repositories to scan.")] = 12,
    commits: Annotated[int, typer.Option(min=1, max=100, help="Commits per repository.")] = 100,
) -> None:
    """Read work addresses out of public commit metadata, at no cost per result."""

    container = state(ctx).container
    require_initialized(container)
    connector = cast(
        GitHubCommitEmailConnector,
        container.connectors.require("people.github_commits"),
    )
    found = connector.find_addresses(
        org,
        domain=domain,
        max_repositories=repos,
        max_commits_per_repository=commits,
    )
    table = Table(title=f"Commit authors at {org}" + (f" (@{domain})" if domain else ""))
    table.add_column("Commits", justify="right")
    table.add_column("Name")
    table.add_column("Address")
    table.add_column("Citation")
    for record in found:
        table.add_row(
            str(record.commits),
            record.name or "unknown",
            record.email,
            record.citation_url,
        )
    console.print(table)
    console.print(
        f"{len(found)} addresses, published by their owners in commit metadata. "
        f"{connector.remaining_requests()} API requests left this hour. No cost."
    )


@contact_app.command("pages")
def contact_pages(
    ctx: typer.Context,
    domain: Annotated[str, typer.Argument(help="Company mail domain, for example acme.com.")],
    url: Annotated[
        str | None,
        typer.Option(help="Read one known page instead of sweeping conventional paths."),
    ] = None,
) -> None:
    """Read addresses a company publishes on its own site, honouring robots.txt."""

    container = state(ctx).container
    require_initialized(container)
    connector = cast(
        CompanyPageContactConnector,
        container.connectors.require("people.company_pages"),
    )
    found = (
        connector.find_contacts_at(url, domain=domain) if url else connector.find_contacts(domain)
    )
    table = Table(title=f"Published on {domain}")
    table.add_column("Address")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Page")
    for contact in found:
        table.add_row(
            contact.email,
            contact.name or "not stated",
            "role mailbox" if contact.is_role_address else "individual",
            contact.page_url,
        )
    console.print(table)
    console.print(f"{len(found)} addresses, each citing the page that prints it. No cost.")
