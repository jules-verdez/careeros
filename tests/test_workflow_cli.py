from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import FIXED_NOW, PROJECT_ROOT
from sqlalchemy import func, select
from test_application_workflow import snapshot, verified
from test_opportunities import add_role
from typer.testing import CliRunner

from careeros.cli import CliState, app
from careeros.clock import FixedClock
from careeros.config import Settings
from careeros.container import Container
from careeros.database.enums import ActionStatus
from careeros.database.models import ActionRequest, Application, CandidateVersion, Email
from careeros.migrations import upgrade_database
from careeros.services.normalization import hash_payload

runner = CliRunner()


@pytest.fixture
def operator(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'operator.db'}"
    upgrade_database(url)
    container = Container.build(
        Settings(database_url=url),
        clock=FixedClock(FIXED_NOW),
        prompt_root=PROJECT_ROOT / "prompts",
    )
    monkeypatch.setattr(CliState, "container", property(lambda self: container))
    monkeypatch.setattr(CliState, "close", lambda self: None)

    class Connector:
        name = "fixture-vacancy"

        def check(self, url, *, title, company):
            assert title == "Researcher" and company == "Example Capital"
            return verified(container, review=False)

    def require(capability):
        assert capability == "jobs.verification", "Unexpected external provider access"
        return Connector()

    monkeypatch.setattr(container.connectors, "require", require)

    def invoke(*args, code=0):
        result = runner.invoke(app, ["--database-url", url, *args])
        assert result.exit_code == code, f"{args}\n{result.output}\n{result.exception}"
        return result.output

    invoke("init")
    yield container, invoke
    container.database.dispose()


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def test_complete_cli_workflow_and_exports(operator, tmp_path):
    container, invoke = operator
    job = add_role(container)
    first = json.loads(
        invoke(
            "profile",
            "import",
            "--file",
            write(tmp_path, "profile.json", snapshot().model_dump(mode="json")),
            "--reviewer",
            "Demo Candidate",
        )
    )
    assert first["version"] == 1
    assert len(json.loads(invoke("profile", "versions"))) == 1
    invoke(
        "assess",
        job,
        "--fit",
        "0.9",
        "--confidence",
        "0.8",
        "--competition",
        "0.5",
        "--why",
        "Verified evidence",
    )
    checked = json.loads(
        invoke("vacancy", "check", job, "--official-url", "https://example.test/jobs/researcher")
    )
    assert checked["status"] == "open" and checked["reviewed_by"] is None
    invoke(
        "vacancy",
        "record",
        job,
        "--file",
        write(tmp_path, "verified.json", verified(container).model_dump(mode="json")),
        "--reviewer",
        "Demo Candidate",
    )
    assert len(json.loads(invoke("vacancy", "show", job))) == 2
    tracked = json.loads(
        invoke(
            "application", "track", job, "--role-family", "Research", "--source", "Employer board"
        )
    )
    application = tracked["id"]
    assert json.loads(invoke("application", "track", job))["id"] == application
    assert "Researcher" in invoke("application", "list")
    assert len(json.loads(invoke("application", "list", "--due", "--json"))) == 1
    invoke(
        "application",
        "next",
        application,
        "--action",
        "Review packet",
        "--at",
        (FIXED_NOW + timedelta(days=3)).isoformat(),
    )
    assert json.loads(invoke("application", "list", "--due", "--json")) == []
    specification = write(
        tmp_path,
        "spec.json",
        {
            "evidence_keys": ["python", "project"],
            "answers": [{"question": "Relevant project?", "evidence_keys": ["project"]}],
        },
    )
    packet = json.loads(invoke("packet", "prepare", application, "--spec", specification))[
        "packet_id"
    ]
    assert "Built Python" in invoke("packet", "show", packet)
    export = tmp_path / "pack"
    invoke("packet", "show", packet, "--export", str(export))
    manifest = json.loads((export / "manifest.json").read_text())
    assert hash_payload(manifest["payload"]) == manifest["sha256"]
    assert (export / "cv.md").read_text() == manifest["payload"]["cv_markdown"]
    invoke("packet", "show", packet, "--export", str(export), code=2)
    action = json.loads(invoke("packet", "request-approval", packet))["action_id"]
    assert "submit_application" in invoke("action", "show", action)
    invoke("action", "approve", action)
    assert "no executable handler" in invoke("action", "execute", action, code=2)
    with container.database.session() as session:
        assert session.get(Application, application).applied_at is None
        assert session.get(ActionRequest, action).status == ActionStatus.APPROVED
        assert session.scalar(select(func.count(Email.id))) == 0
    invoke(
        "application",
        "record-submission",
        application,
        "--at",
        FIXED_NOW.isoformat(),
        "--receipt",
        "ATS receipt 123",
        "--packet",
        packet,
    )
    invoke(
        "application",
        "next",
        application,
        "--action",
        "Check response",
        "--at",
        FIXED_NOW.isoformat(),
        "--followup",
    )
    assert "Follow up:" in invoke("followup")
    invoke(
        "application",
        "stage",
        application,
        "screening",
        "--evidence",
        "Employer screening invitation",
    )
    plan = write(
        tmp_path,
        "plan.json",
        {
            "format": "Technical screen",
            "logistics": "Employer video link",
            "evidence_keys": ["python"],
            "practice_questions": ["Explain a project"],
        },
    )
    meeting = json.loads(
        invoke(
            "interview",
            "schedule",
            application,
            "--start",
            (FIXED_NOW + timedelta(days=1)).isoformat(),
            "--end",
            (FIXED_NOW + timedelta(days=1, hours=1)).isoformat(),
            "--timezone",
            "Europe/London",
            "--plan",
            plan,
        )
    )["id"]
    assert "Built Python" in invoke("interview", "show", meeting)
    invoke(
        "interview",
        "reschedule",
        meeting,
        "--start",
        (FIXED_NOW - timedelta(hours=2)).isoformat(),
        "--end",
        (FIXED_NOW - timedelta(hours=1)).isoformat(),
        "--timezone",
        "Europe/London",
    )
    debrief = write(
        tmp_path,
        "debrief.json",
        {
            "reflection": "Improve the example",
            "employer_feedback": "Next round pending",
            "next_step": "Practise SQL",
        },
    )
    invoke("interview", "debrief", meeting, "--file", debrief)
    second_meeting = json.loads(
        invoke(
            "interview",
            "schedule",
            application,
            "--start",
            (FIXED_NOW + timedelta(days=3)).isoformat(),
            "--end",
            (FIXED_NOW + timedelta(days=3, hours=1)).isoformat(),
            "--timezone",
            "Europe/London",
            "--plan",
            plan,
        )
    )["id"]
    invoke("interview", "cancel", second_meeting, "--reason", "Employer rescheduling")
    invoke(
        "application",
        "stage",
        application,
        "rejected",
        "--evidence",
        "Employer selected another candidate",
        "--reflection",
        "Practise live coding",
    )
    detail = json.loads(invoke("application", "show", application))
    assert (
        detail["application"]["status"] == "rejected"
        and detail["submissions"][0]["receipt"] == "ATS receipt 123"
    )
    report = json.loads(
        invoke("application", "funnel", "--since", (FIXED_NOW - timedelta(days=30)).isoformat())
    )
    assert report["groups"][0]["submitted"] == 1
    second = json.loads(
        invoke(
            "profile",
            "import",
            "--file",
            write(tmp_path, "changed.json", snapshot("Updated headline").model_dump(mode="json")),
            "--reviewer",
            "Demo Candidate",
        )
    )
    assert '+  "headline": "Updated headline"' in invoke(
        "profile", "diff", first["id"], second["id"]
    )
    assert "Audit chain valid" in invoke("audit", "verify")


def test_invalid_cli_input_rolls_back_and_reports_actionable_error(operator, tmp_path):
    container, invoke = operator
    assert "not found" in invoke("application", "track", "missing", code=2)
    invalid = write(
        tmp_path, "invalid.json", {"name": "Demo Candidate", "headline": "", "facts": []}
    )
    assert (
        "validation"
        in invoke(
            "profile", "import", "--file", invalid, "--reviewer", "Demo Candidate", code=2
        ).lower()
    )
    with container.database.session() as session:
        assert session.scalar(select(func.count(CandidateVersion.id))) == 0
