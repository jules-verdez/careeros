from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from careeros.cli import app
from careeros.database.base import Base
from careeros.database.enums import JobStatus
from careeros.database.models import Job, JobAssessment
from careeros.database.session import Database

runner = CliRunner()


def test_migrated_cli_preserves_evidence_in_json_and_all_operator_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'career.db'}"
    base = ["--database-url", database_url]
    initialized = runner.invoke(app, [*base, "init"])
    assert initialized.exit_code == 0, initialized.output
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            [
                {
                    "source_kind": "greenhouse",
                    "source_name": "Example Careers",
                    "external_id": str(index),
                    "source_url": f"https://example.test/roles/{index}",
                    "company_name": "Example Capital",
                    "title": title,
                    "location": "London, UK",
                }
                for index, title in enumerate(["Researcher", "Engineer", "Analyst"])
            ]
        )
    )
    discovered = runner.invoke(app, [*base, "discover", "--from-json", str(jobs_file)])
    assert discovered.exit_code == 0, discovered.output
    review_file = tmp_path / "review.json"
    review_file.write_text(
        json.dumps(
            {
                "eligibility_label": "currently_ineligible",
                "unresolved_fact": "This vacancy excludes sponsorship.",
                "evidence": ["Employer: no sponsorship is available."],
                "canonical_url": "https://example.test/roles/0",
                "next_verification_step": "Verify whether the employer restriction has changed.",
            }
        )
    )
    database = Database(database_url)
    try:
        with database.session() as session:
            jobs = {job.title: job.id for job in session.scalars(select(Job))}
        for title, fit in [("Researcher", "0.9"), ("Engineer", "0.8"), ("Analyst", "0.75")]:
            extra = ["--review-file", str(review_file)] if title == "Researcher" else []
            result = runner.invoke(
                app,
                [
                    *base,
                    "assess",
                    jobs[title],
                    "--fit",
                    fit,
                    "--confidence",
                    "0.9",
                    "--competition",
                    "0.5",
                    "--why",
                    "Assessed against the CV.",
                    *extra,
                ],
            )
            assert result.exit_code == 0, result.output

        def fail_connector(*args: object, **kwargs: object) -> None:
            pytest.fail("A saved-evidence view must not request a provider connector")

        monkeypatch.setattr(
            "careeros.connectors.registry.ConnectorRegistry.require", fail_connector
        )
        with database.session() as session:
            before = {
                table.name: session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
        result = runner.invoke(app, [*base, "opportunities", "--json", "--limit", "1"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["total_assessed_jobs"] == 3
        assert len(payload["opportunities"]) == 1
        assert len(payload["mandatory_user_review"]) == 2
        card = next(
            card for card in payload["mandatory_user_review"] if card["title"] == "Researcher"
        )
        assert card["profile_fit_score"] == 0.9
        assert card["eligibility_label"] == "currently_ineligible"
        assert card["canonical_url"] == "https://example.test/roles/0"
        assert card["evidence"] == ["Employer: no sponsorship is available."]
        with database.session() as session:
            after = {
                table.name: session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
            assert all(job.status == JobStatus.DISCOVERED for job in session.scalars(select(Job)))
        assert before == after

        for command in [
            ["opportunities", "--limit", "1"],
            ["jobs", "--sponsor-only", "--limit", "1"],
            ["review", "--limit", "1"],
            ["dashboard"],
        ]:
            rendered = runner.invoke(app, [*base, *command])
            assert rendered.exit_code == 0, rendered.output
            assert "mandatory_user_review (2)" in rendered.output
            assert "currently_ineligible" in rendered.output
            assert "Engineer" in rendered.output
            assert "Researcher" in rendered.output
            if command[0] in {"review", "dashboard"}:
                assert "Saved brief for" in rendered.output
                assert "Today's state" not in rendered.output

        with database.session() as session:
            saved = session.scalar(
                select(JobAssessment).where(JobAssessment.job_id == jobs["Researcher"])
            )
            original = saved.explanation_json
        review_file.write_text(
            json.dumps(
                {
                    "eligibility_label": "confirmed",
                    "canonical_url": "https://example.test/roles/0",
                    "next_verification_step": "Recheck before applying.",
                }
            )
        )
        invalid = runner.invoke(
            app,
            [
                *base,
                "assess",
                jobs["Researcher"],
                "--fit",
                "0.9",
                "--confidence",
                "0.9",
                "--competition",
                "0.5",
                "--why",
                "Unsupported conclusion.",
                "--review-file",
                str(review_file),
            ],
        )
        assert invalid.exit_code == 2
        with database.session() as session:
            assert session.get(JobAssessment, saved.id).explanation_json == original
        verified = runner.invoke(app, [*base, "audit", "verify"])
        assert verified.exit_code == 0, verified.output
    finally:
        database.dispose()
