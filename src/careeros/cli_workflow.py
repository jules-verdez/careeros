"""Operator commands for applications, candidate evidence, verification and interviews."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from careeros.container import Container
from careeros.database.enums import ApplicationStatus
from careeros.database.models import (
    Application,
    ApplicationEvent,
    ApplicationPacket,
    ApplicationSubmission,
    ApplicationWorkspace,
    CandidateProfile,
    CandidateVersion,
    Company,
    InterviewWorkspace,
    Job,
    Meeting,
    VacancyVerification,
)
from careeros.domain.workflow import (
    CandidateSnapshot,
    InterviewDebrief,
    InterviewPlan,
    PacketSpec,
    VacancyEvidence,
    instant,
    utc,
)
from careeros.services.applications import require

console = Console()
applications_app = typer.Typer(help="Track local applications, receipts, deadlines and outcomes.")
packets_app = typer.Typer(help="Prepare and review frozen application materials.")
vacancies_app = typer.Typer(help="Check official finalist vacancies and record human evidence.")
interviews_app = typer.Typer(help="Manage interview logistics, preparation and debriefs.")


@contextmanager
def workflow(ctx: typer.Context) -> Iterator[tuple[Container, Session, str]]:
    from careeros.cli import active_profile, require_initialized, state

    container = state(ctx).container
    require_initialized(container)
    if not inspect(container.database.engine).has_table("application_workspaces"):
        raise typer.BadParameter(
            "Apply the application-workflow migration with `career init` first"
        )
    profile_id = active_profile(container).id
    try:
        with container.database.session() as session:
            yield container, session, profile_id
    except (ValueError, LookupError, OSError) as error:
        raise typer.BadParameter(str(error)) from error


def output(value: Any) -> None:
    def default(item: Any) -> str:
        if isinstance(item, datetime):
            return utc(item).isoformat()
        raise TypeError(f"Cannot serialize {type(item).__name__}")

    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=default))


def fields(model: Any) -> dict[str, Any]:
    return {column.key: getattr(model, column.key) for column in inspect(type(model)).columns}


def own_application(session: Session, application_id: str, profile_id: str) -> Application:
    app = require(session, Application, application_id)
    workspace = require(session, ApplicationWorkspace, application_id)
    if workspace.candidate_profile_id != profile_id:
        raise ValueError("Application belongs to another candidate profile")
    return app


def operator_name(session: Session, profile_id: str) -> str:
    return require(session, CandidateProfile, profile_id).name


@applications_app.command("track")
def track(
    ctx: typer.Context, job_id: str, role_family: str = "unclassified", source: str = "unclassified"
) -> None:
    """Start tracking a role, or adopt an existing application without duplicating it."""
    with workflow(ctx) as (container, session, profile_id):
        app = container.applications.start(
            session,
            job_id=job_id,
            profile_id=profile_id,
            actor="career.application.track",
            role_family=role_family,
            source_label=source,
        )
        output(fields(app))


@applications_app.command("list")
def applications_list(
    ctx: typer.Context, due: bool = False, as_json: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show applications; --due limits the view to unfinished actions due now."""
    with workflow(ctx) as (container, session, profile_id):
        container.applications.reconcile(session)
        rows = session.execute(
            select(Application, ApplicationWorkspace, Job, Company)
            .join(ApplicationWorkspace, ApplicationWorkspace.application_id == Application.id)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
            .where(ApplicationWorkspace.candidate_profile_id == profile_id)
            .order_by(ApplicationWorkspace.next_action_at.asc().nullslast(), Application.id)
        ).all()
        items = []
        from careeros.database.enums import TaskStatus
        from careeros.database.models import ApplicationTask, Task

        for application, workspace, job, company in rows:
            due_tasks = session.scalars(
                select(Task)
                .join(ApplicationTask)
                .where(
                    ApplicationTask.application_id == application.id,
                    Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]),
                    Task.due_at <= container.clock.now(),
                )
            ).all()
            if due and not due_tasks:
                continue
            items.append(
                {
                    "id": application.id,
                    "company": company.canonical_name,
                    "title": job.title,
                    "status": application.status.value,
                    "next_action": workspace.next_action,
                    "next_action_at": workspace.next_action_at,
                    "deadline_at": workspace.deadline_at,
                    "due_tasks": [task.title for task in due_tasks],
                }
            )
        if as_json:
            output(items)
        else:
            table = Table(title="Applications" + (" needing attention" if due else ""))
            for name in ("ID", "Company / role", "Stage", "Next action", "Due (UTC)"):
                table.add_column(name)
            for item in items:
                table.add_row(
                    item["id"],
                    f"{item['company']} / {item['title']}",
                    item["status"],
                    item["next_action"] or "—",
                    str(item["next_action_at"] or "—"),
                )
            console.print(table)


@applications_app.command("show")
def application_show(ctx: typer.Context, application_id: str) -> None:
    """Show the application timeline, next action, and recorded submission evidence."""
    with workflow(ctx) as (_, session, profile_id):
        app = own_application(session, application_id, profile_id)
        events = session.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == application_id)
            .order_by(ApplicationEvent.position)
        ).all()
        receipts = session.scalars(
            select(ApplicationSubmission).where(
                ApplicationSubmission.application_id == application_id
            )
        ).all()
        output(
            {
                "application": fields(app),
                "workspace": fields(require(session, ApplicationWorkspace, application_id)),
                "timeline": [fields(event) for event in events],
                "submissions": [fields(row) for row in receipts],
            }
        )


@applications_app.command("next")
def next_action(
    ctx: typer.Context,
    application_id: str,
    action: Annotated[str, typer.Option()],
    at: Annotated[str, typer.Option()],
    deadline: str | None = None,
    followup: bool = False,
) -> None:
    """Set or snooze the next action. Include a UTC offset in every date."""
    with workflow(ctx) as (container, session, profile_id):
        own_application(session, application_id, profile_id)
        container.applications.schedule(
            session,
            application_id,
            action=action,
            due_at=instant(at),
            deadline=instant(deadline) if deadline else None,
            followup=followup,
            actor=operator_name(session, profile_id),
        )
        output({"application_id": application_id, "next_action": action, "due_at": instant(at)})


@applications_app.command("record-submission")
def record_submission(
    ctx: typer.Context,
    application_id: str,
    at: Annotated[str, typer.Option()],
    receipt: Annotated[str, typer.Option()],
    packet: str | None = None,
) -> None:
    """Record an application already submitted by you. This command does not submit anything."""
    with workflow(ctx) as (container, session, profile_id):
        own_application(session, application_id, profile_id)
        submission = container.applications.submit_record(
            session,
            application_id,
            submitted_at=instant(at),
            receipt=receipt,
            packet_id=packet,
            actor=operator_name(session, profile_id),
        )
        output(fields(submission))


@applications_app.command("stage")
def stage(
    ctx: typer.Context,
    application_id: str,
    status: ApplicationStatus,
    evidence: Annotated[str, typer.Option()],
    reflection: str = "",
) -> None:
    """Record screening, interview, offer or closure with evidence separate from reflection."""
    with workflow(ctx) as (container, session, profile_id):
        own_application(session, application_id, profile_id)
        result = container.applications.stage(
            session,
            application_id,
            status=status,
            evidence=evidence,
            reflection=reflection,
            actor=operator_name(session, profile_id),
        )
        output(fields(result))


@applications_app.command("funnel")
def funnel(ctx: typer.Context, since: str | None = None) -> None:
    """Report cohort counts by source and role family, including pending applications."""
    with workflow(ctx) as (container, session, profile_id):
        output(
            container.applications.funnel(
                session, profile_id=profile_id, since=instant(since) if since else None
            )
        )


def profile_import(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    reviewer: Annotated[str, typer.Option()],
) -> None:
    """Import reviewed JSON evidence as an immutable candidate version."""
    with workflow(ctx) as (container, session, profile_id):
        snapshot = CandidateSnapshot.model_validate_json(file.read_text())
        version = container.packets.import_profile(session, profile_id, snapshot, reviewer=reviewer)
        output(fields(version))


def profile_versions(ctx: typer.Context) -> None:
    with workflow(ctx) as (_, session, profile_id):
        versions = session.scalars(
            select(CandidateVersion)
            .where(CandidateVersion.candidate_profile_id == profile_id)
            .order_by(CandidateVersion.version)
        ).all()
        output([fields(version) for version in versions])


def profile_diff(ctx: typer.Context, before: str, after: str) -> None:
    """Display factual changes between two stored candidate version IDs."""
    with workflow(ctx) as (_, session, profile_id):
        versions = [require(session, CandidateVersion, identity) for identity in (before, after)]
        if any(version.candidate_profile_id != profile_id for version in versions):
            raise ValueError("Candidate version belongs to another profile")
        texts = [
            json.dumps(version.payload_json, indent=2, ensure_ascii=False).splitlines(True)
            for version in versions
        ]
        typer.echo("".join(difflib.unified_diff(texts[0], texts[1], fromfile=before, tofile=after)))


@packets_app.command("prepare")
def packet_prepare(
    ctx: typer.Context,
    application_id: str,
    spec: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Assemble a CV and answers from selected, reviewed evidence; save the exact pack."""
    with workflow(ctx) as (container, session, profile_id):
        own_application(session, application_id, profile_id)
        packet = container.packets.prepare(
            session,
            application_id,
            PacketSpec.model_validate_json(spec.read_text()),
            actor=operator_name(session, profile_id),
        )
        output({"packet_id": packet.id, "sha256": packet.sha256, "application_id": application_id})


@packets_app.command("show")
def packet_show(
    ctx: typer.Context,
    packet_id: str,
    export: Annotated[
        Path | None, typer.Option(help="New directory for CV, answers, manifest and review pack.")
    ] = None,
) -> None:
    with workflow(ctx) as (container, session, profile_id):
        packet = require(session, ApplicationPacket, packet_id)
        own_application(session, packet.application_id, profile_id)
        markdown = container.packets.render(packet)
        if export:
            export.mkdir(parents=True, exist_ok=False)
            (export / "review.md").write_text(markdown, encoding="utf-8")
            (export / "cv.md").write_text(packet.payload_json["cv_markdown"], encoding="utf-8")
            (export / "answers.json").write_text(
                json.dumps(packet.payload_json["answers"], indent=2), encoding="utf-8"
            )
            (export / "manifest.json").write_text(
                json.dumps(
                    {
                        "packet_id": packet.id,
                        "sha256": packet.sha256,
                        "payload": packet.payload_json,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            typer.echo(f"Exported {packet.id} to {export}")
        else:
            typer.echo(markdown)


@packets_app.command("request-approval")
def packet_approval(ctx: typer.Context, packet_id: str) -> None:
    """Stage a submission review for the exact pack; no form or browser is opened."""
    with workflow(ctx) as (container, session, profile_id):
        packet = require(session, ApplicationPacket, packet_id)
        own_application(session, packet.application_id, profile_id)
        action = container.packets.request_approval(
            session, packet_id, actor=operator_name(session, profile_id)
        )
        output(
            {
                "action_id": action.id,
                "status": action.status.value,
                "note": "Manual submission only after approval; record its receipt separately.",
            }
        )


@vacancies_app.command("check")
def vacancy_check(
    ctx: typer.Context, job_id: str, official_url: Annotated[str, typer.Option()]
) -> None:
    """Fetch one operator-selected official vacancy URL; retain uncertain results."""
    with workflow(ctx) as (container, session, _):
        job = require(session, Job, job_id)
        company = require(session, Company, job.company_id)
        title, company_name = job.title, company.canonical_name
        connector = container.connectors.require("jobs.verification")
    try:
        evidence = connector.check(official_url, title=title, company=company_name)
    except (ValueError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    with workflow(ctx) as (container, session, profile_id):
        verification = container.vacancies.record(
            session,
            job_id=job_id,
            profile_id=profile_id,
            evidence=evidence,
            actor="career.vacancy.check",
            connector_name=connector.name,
        )
        output(fields(verification))


@vacancies_app.command("record")
def vacancy_record(
    ctx: typer.Context,
    job_id: str,
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    reviewer: Annotated[str, typer.Option()],
) -> None:
    """Record a human-checked official posting, with optional evidenced eligibility review."""
    with workflow(ctx) as (container, session, profile_id):
        verification = container.vacancies.record(
            session,
            job_id=job_id,
            profile_id=profile_id,
            evidence=VacancyEvidence.model_validate_json(file.read_text()),
            actor=reviewer,
            reviewed_by=reviewer,
        )
        output(fields(verification))


@vacancies_app.command("show")
def vacancy_show(ctx: typer.Context, job_id: str) -> None:
    with workflow(ctx) as (container, session, profile_id):
        records = session.scalars(
            select(VacancyVerification)
            .where(
                VacancyVerification.job_id == job_id,
                VacancyVerification.candidate_profile_id == profile_id,
            )
            .order_by(VacancyVerification.checked_at.desc())
        ).all()
        output(
            [{**fields(record), "fresh": container.vacancies.current(record)} for record in records]
        )


@interviews_app.command("schedule")
def interview_schedule(
    ctx: typer.Context,
    application_id: str,
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    timezone: Annotated[str, typer.Option()],
    plan: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Record interview logistics locally and prepare examples. No calendar invitation is sent."""
    with workflow(ctx) as (container, session, profile_id):
        own_application(session, application_id, profile_id)
        meeting = container.interviews.schedule(
            session,
            application_id,
            starts_at=instant(start),
            ends_at=instant(end),
            timezone=timezone,
            plan=InterviewPlan.model_validate_json(plan.read_text()),
            actor=operator_name(session, profile_id),
        )
        container.planner.reconcile(session, profile_id=profile_id, actor="career.interview")
        output(fields(meeting))


@interviews_app.command("show")
def interview_show(ctx: typer.Context, meeting_id: str) -> None:
    with workflow(ctx) as (_, session, profile_id):
        meeting = require(session, Meeting, meeting_id)
        own_application(session, meeting.application_id or "", profile_id)
        output(
            {
                "meeting": fields(meeting),
                "preparation": fields(require(session, InterviewWorkspace, meeting_id)),
            }
        )


@interviews_app.command("reschedule")
def interview_reschedule(
    ctx: typer.Context,
    meeting_id: str,
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    timezone: Annotated[str, typer.Option()],
) -> None:
    with workflow(ctx) as (container, session, profile_id):
        meeting = require(session, Meeting, meeting_id)
        own_application(session, meeting.application_id or "", profile_id)
        result = container.interviews.reschedule(
            session,
            meeting_id,
            starts_at=instant(start),
            ends_at=instant(end),
            timezone=timezone,
            actor=operator_name(session, profile_id),
        )
        output(fields(result))


@interviews_app.command("debrief")
def interview_debrief(
    ctx: typer.Context,
    meeting_id: str,
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    with workflow(ctx) as (container, session, profile_id):
        meeting = require(session, Meeting, meeting_id)
        own_application(session, meeting.application_id or "", profile_id)
        container.interviews.debrief(
            session,
            meeting_id,
            InterviewDebrief.model_validate_json(file.read_text()),
            actor=operator_name(session, profile_id),
        )
        output({"meeting_id": meeting_id, "status": meeting.status.value})


@interviews_app.command("cancel")
def interview_cancel(
    ctx: typer.Context, meeting_id: str, reason: Annotated[str, typer.Option()]
) -> None:
    with workflow(ctx) as (container, session, profile_id):
        meeting = require(session, Meeting, meeting_id)
        own_application(session, meeting.application_id or "", profile_id)
        container.interviews.cancel(
            session, meeting_id, reason=reason, actor=operator_name(session, profile_id)
        )
        output({"meeting_id": meeting_id, "status": meeting.status.value})


def register_workflow(app: typer.Typer, profile_app: typer.Typer) -> None:
    app.add_typer(applications_app, name="application")
    app.add_typer(packets_app, name="packet")
    app.add_typer(vacancies_app, name="vacancy")
    app.add_typer(interviews_app, name="interview")
    profile_app.command("import")(profile_import)
    profile_app.command("versions")(profile_versions)
    profile_app.command("diff")(profile_diff)
