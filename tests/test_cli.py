from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from careeros.cli import app
from careeros.database.models import (
    ActionRequest,
    Job,
    Person,
    ResearchReport,
    Task,
)
from careeros.database.session import Database

runner = CliRunner()


def test_cli_initializes_discovers_reviews_and_fails_closed_for_mail(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'cli.db'}"
    base = ["--database-url", database_url]
    initialized = runner.invoke(app, [*base, "init", "--email", "candidate@example.com"])
    assert initialized.exit_code == 0, initialized.output
    assert "CareerOS initialized" in initialized.output

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "source_kind": "greenhouse",
                    "source_name": "Example",
                    "external_id": "1",
                    "source_url": "https://boards.greenhouse.io/example/jobs/1",
                    "company_name": "Example Capital",
                    "company_domain": "example.test",
                    "title": "Quantitative Researcher",
                    "location": "London, UK",
                    "posted_at": "2026-07-30T08:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    discovered = runner.invoke(
        app,
        [*base, "discover", "--from-json", str(jobs_path)],
    )
    assert discovered.exit_code == 0, discovered.output
    assert "jobs created: 1" in discovered.output

    reviewed = runner.invoke(app, [*base, "review"])
    assert reviewed.exit_code == 0, reviewed.output
    assert "Good morning Candidate" in reviewed.output
    assert "Recommended actions" in reviewed.output

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "careeros.connectors.gmail.shutil.which",
        lambda command: None if command == "gws" else f"/bin/{command}",
    )
    inbox = runner.invoke(app, [*base, "inbox"])
    assert inbox.exit_code == 2
    assert "data was read" in inbox.output

    audit = runner.invoke(app, [*base, "audit", "verify"])
    assert audit.exit_code == 0, audit.output
    assert "Audit chain valid" in audit.output


def test_cli_full_operator_path_stages_but_does_not_execute(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'operator.db'}"
    base = ["--database-url", database_url]
    assert (
        runner.invoke(
            app,
            [*base, "init", "--email", "candidate@example.com"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, [*base, "profile", "show"]).exit_code == 0
    preference = runner.invoke(
        app,
        [
            *base,
            "profile",
            "preference",
            "--kind",
            "role",
            "--weight",
            "1.0",
            "--required",
            "Quant Researcher",
        ],
    )
    assert preference.exit_code == 0, preference.output

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "source_kind": "greenhouse",
                    "source_name": "Example",
                    "external_id": "1",
                    "source_url": "https://boards.greenhouse.io/example/jobs/1",
                    "company_name": "Example Capital",
                    "company_domain": "example.test",
                    "title": "Quantitative Researcher",
                    "location": "London, UK",
                    "posted_at": "2026-07-30T08:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            [*base, "discover", "--from-json", str(jobs_path)],
        ).exit_code
        == 0
    )
    database = Database(database_url)
    try:
        with database.session() as session:
            job = session.scalar(select(Job))
            assert job is not None
            job_id = job.id
        jobs = runner.invoke(app, [*base, "jobs"])
        assert jobs.exit_code == 0, jobs.output
        assessed = runner.invoke(
            app,
            [
                *base,
                "assess",
                job_id,
                "--fit",
                "0.9",
                "--confidence",
                "0.8",
                "--competition",
                "0.6",
                "--why",
                "Verified human assessment.",
            ],
        )
        assert assessed.exit_code == 0, assessed.output
        assert runner.invoke(app, [*base, "shortlist", job_id]).exit_code == 0

        markdown_path = tmp_path / "research.md"
        markdown_path.write_text("# Example Capital\n\nVerified public facts.", encoding="utf-8")
        json_path = tmp_path / "research.json"
        json_path.write_text(
            json.dumps({"claims": [], "unknowns": []}),
            encoding="utf-8",
        )
        research = runner.invoke(
            app,
            [
                *base,
                "research",
                "Example Capital",
                "--markdown",
                str(markdown_path),
                "--json",
                str(json_path),
                "--as-of",
                "2026-07-30",
                "--confidence",
                "0.85",
            ],
        )
        assert research.exit_code == 0, research.output

        person_added = runner.invoke(
            app,
            [
                *base,
                "person",
                "add",
                "--company",
                "Example Capital",
                "--name",
                "Sample Contact",
                "--title",
                "Head of Quant",
                "--influence",
                "0.9",
                "--confidence",
                "0.8",
                "--email",
                "recipient@example.test",
                "--email-source-url",
                "https://example.test/team/contact",
            ],
        )
        assert person_added.exit_code == 0, person_added.output
        people = runner.invoke(app, [*base, "person", "list"])
        assert people.exit_code == 0, people.output
        assert "Sample" in people.output
        assert "Example" in people.output

        with database.session() as session:
            person = session.scalar(select(Person))
            report = session.scalar(select(ResearchReport))
            assert person is not None
            assert report is not None
            person_id = person.id
            evidence_id = report.markdown_document_id

        body_path = tmp_path / "outreach.txt"
        body_path.write_text(
            "I read the public research.\n\nThis is a fictional follow-up request.",
            encoding="utf-8",
        )
        outreach_args = [
            *base,
            "outreach",
            person_id,
            "--subject",
            "Quant research",
            "--body",
            str(body_path),
            "--why",
            "Establishes cited relevance.",
            "--why",
            "Makes one specific request.",
            "--evidence",
            evidence_id,
        ]
        first_outreach = runner.invoke(app, outreach_args)
        assert first_outreach.exit_code == 0, first_outreach.output
        assert "Nothing was sent" in first_outreach.output

        with database.session() as session:
            first_action = session.scalar(
                select(ActionRequest).order_by(ActionRequest.requested_at)
            )
            assert first_action is not None
            first_action_id = first_action.id
        listed = runner.invoke(app, [*base, "action", "list", "--status", "pending"])
        assert listed.exit_code == 0, listed.output
        approved = runner.invoke(
            app,
            [*base, "action", "approve", first_action_id, "--reviewer", "Demo Candidate"],
        )
        assert approved.exit_code == 0, approved.output
        assert "No external action was executed" in approved.output

        second_outreach = runner.invoke(app, outreach_args)
        assert second_outreach.exit_code == 0, second_outreach.output
        with database.session() as session:
            pending = session.scalar(
                select(ActionRequest)
                .where(ActionRequest.status == "PENDING")
                .order_by(ActionRequest.requested_at.desc())
            )
            assert pending is not None
            pending_id = pending.id
        rejected = runner.invoke(
            app,
            [
                *base,
                "action",
                "reject",
                pending_id,
                "--reason",
                "Needs revision",
                "--reviewer",
                "Demo Candidate",
            ],
        )
        assert rejected.exit_code == 0, rejected.output

        reviewed = runner.invoke(app, [*base, "review", "--limit", "5"])
        assert reviewed.exit_code == 0, reviewed.output
        dashboard = runner.invoke(app, [*base, "dashboard"])
        assert dashboard.exit_code == 0, dashboard.output
        followups = runner.invoke(app, [*base, "followup"])
        assert followups.exit_code == 0, followups.output
        tasks = runner.invoke(app, [*base, "task", "list"])
        assert tasks.exit_code == 0, tasks.output
        with database.session() as session:
            task = session.scalar(select(Task))
            assert task is not None
            task_id = task.id
        completed = runner.invoke(app, [*base, "task", "done", task_id])
        assert completed.exit_code == 0, completed.output
        assert runner.invoke(app, [*base, "db", "current"]).exit_code == 0
        assert runner.invoke(app, [*base, "audit", "verify"]).exit_code == 0
    finally:
        database.dispose()
