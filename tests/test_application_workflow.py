from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_opportunities import add_role, assess

from careeros.clock import FixedClock
from careeros.container import Container
from careeros.database.enums import (
    ActionStatus,
    ApplicationStatus,
    JobStatus,
    MeetingStatus,
    TaskStatus,
)
from careeros.database.models import (
    ActionRequest,
    Application,
    ApplicationEvent,
    ApplicationPacket,
    ApplicationSubmission,
    ApplicationTask,
    ApplicationWorkspace,
    CandidateProfile,
    CandidateVersion,
    InterviewWorkspace,
    Job,
    Task,
    VacancyVerification,
)
from careeros.domain.job_review import EligibilityLabel, ReviewEvidence
from careeros.domain.workflow import (
    CandidateFact,
    CandidateSnapshot,
    InterviewDebrief,
    InterviewPlan,
    PacketAnswer,
    PacketSpec,
    VacancyEvidence,
    VacancyStatus,
    instant,
    utc,
)

URL = "https://example.test/jobs/researcher"


def snapshot(headline="Research and data") -> CandidateSnapshot:
    return CandidateSnapshot(
        name="Demo Candidate",
        headline=headline,
        facts=(
            CandidateFact(
                key="python",
                kind="experience",
                text="Built Python reporting tools.",
                source="reviewed-cv.md#experience",
                tags=("Python",),
            ),
            CandidateFact(
                key="project",
                kind="project",
                text="Built an auditable research simulator.",
                source="https://example.test/project",
            ),
        ),
    )


def verified(
    container: Container,
    *,
    status=VacancyStatus.OPEN,
    label=EligibilityLabel.CONFIRMED,
    when=None,
    review=True,
) -> VacancyEvidence:
    return VacancyEvidence(
        source_url=URL,
        status=status,
        checked_at=when or container.clock.now(),
        deadline_at=container.clock.now() + timedelta(days=10),
        evidence=("Employer posting inspected by the reviewer.",),
        review=ReviewEvidence(
            eligibility_label=label,
            canonical_url=URL,
            evidence=("Employer confirms sponsorship for this specific vacancy.",),
            unresolved_fact="Employer excludes sponsorship."
            if label == EligibilityLabel.CURRENTLY_INELIGIBLE
            else "",
            next_verification_step="Recheck before applying.",
        )
        if review
        else None,
    )


@pytest.fixture
def ready(container: Container, active_profile: str):
    job_id = add_role(container)
    assess(container, active_profile, job_id)
    with container.database.session() as session:
        app = container.applications.start(
            session,
            job_id=job_id,
            profile_id=active_profile,
            actor="test",
            role_family="Research",
            source_label="Direct",
        )
        version = container.packets.import_profile(
            session, active_profile, snapshot(), reviewer="Demo Candidate"
        )
        verification = container.vacancies.record(
            session,
            job_id=job_id,
            profile_id=active_profile,
            evidence=verified(container),
            actor="Demo Candidate",
            reviewed_by="Demo Candidate",
        )
        packet = container.packets.prepare(
            session,
            app.id,
            PacketSpec(
                evidence_keys=("python",),
                answers=(PacketAnswer(question="Relevant project?", evidence_keys=("project",)),),
            ),
            actor="test",
        )
        return {
            "job": job_id,
            "profile": active_profile,
            "app": app.id,
            "version": version.id,
            "verification": verification.id,
            "packet": packet.id,
        }


def submitted(container, ready):
    with container.database.session() as session:
        container.applications.submit_record(
            session,
            ready["app"],
            submitted_at=container.clock.now(),
            receipt="ATS confirmation reference ABC-123",
            packet_id=ready["packet"],
            actor="Demo Candidate",
        )


def test_complete_local_application_flow_is_idempotent_and_audited(container, ready):
    with container.database.session() as session:
        application = container.applications.start(
            session, job_id=ready["job"], profile_id=ready["profile"], actor="test"
        )
        assert application.id == ready["app"]
        original = session.get(ApplicationPacket, ready["packet"])
        repeat = container.packets.prepare(
            session,
            application.id,
            PacketSpec.model_validate(original.payload_json["specification"]),
            actor="test",
        )
        assert repeat.id == original.id
        action = container.packets.request_approval(session, original.id, actor="test")
        assert (
            container.packets.request_approval(session, original.id, actor="test").id == action.id
        )
        container.approvals.approve(session, action.id, reviewer="Demo Candidate")
        assert action.status == ActionStatus.APPROVED
        assert application.applied_at is None
        assert "Built Python reporting tools." in container.packets.render(original)
        assert (
            original.payload_json["answers"][0]["answer"]
            == "Built an auditable research simulator."
        )
    submitted(container, ready)
    submitted(container, ready)
    with container.database.session() as session:
        assert session.scalar(select(func.count(ApplicationSubmission.id))) == 1
        app = session.get(Application, ready["app"])
        assert app.status == ApplicationStatus.APPLIED
        assert session.get(Job, ready["job"]).status == JobStatus.APPLIED
        container.applications.schedule(
            session,
            app.id,
            action="Check for response",
            due_at=container.clock.now() + timedelta(days=5),
            actor="Demo Candidate",
            followup=True,
        )
        profile = session.get(CandidateProfile, ready["profile"])
        brief = container.briefs.generate(session, profile=profile, actor="test", limit=100)
        assert not any("Follow up:" in item.recommendation for item in brief.items)
        container.applications.schedule(
            session,
            app.id,
            action="Check for response",
            due_at=container.clock.now(),
            actor="Demo Candidate",
            followup=True,
        )
        brief = container.briefs.generate(session, profile=profile, actor="test", limit=100)
        assert any("Follow up:" in item.recommendation for item in brief.items)
        assert container.audit.verify(session).valid
        events = session.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == app.id)
            .order_by(ApplicationEvent.position)
        ).all()
        assert [event.position for event in events] == list(range(1, len(events) + 1))


def test_profile_versions_preserve_old_packets_and_require_fresh_approval(container, ready):
    with container.database.session() as session:
        version = container.packets.import_profile(
            session, ready["profile"], snapshot(), reviewer="Demo Candidate"
        )
        assert version.id == ready["version"]
        changed = container.packets.import_profile(
            session, ready["profile"], snapshot("Data science"), reviewer="Demo Candidate"
        )
        assert changed.version == 2
        assert (
            session.get(ApplicationPacket, ready["packet"]).payload_json["candidate_snapshot"][
                "headline"
            ]
            == "Research and data"
        )
        with pytest.raises(ValueError, match="profile changed"):
            container.packets.request_approval(session, ready["packet"], actor="test")


@pytest.mark.parametrize(
    "failure",
    ["stale", "closed", "unknown", "ineligible", "deadline", "packet", "version", "verification"],
)
def test_submission_approval_fails_closed(container, ready, failure):
    with container.database.session() as session:
        packet = session.get(ApplicationPacket, ready["packet"])
        verification = session.get(VacancyVerification, ready["verification"])
        if failure == "stale":
            container.vacancies.clock = FixedClock(container.clock.now() + timedelta(days=3))
        elif failure == "packet":
            packet.payload_json = {**packet.payload_json, "cv_markdown": "Invented achievement"}
        elif failure == "version":
            version = session.get(CandidateVersion, ready["version"])
            version.payload_json = {**version.payload_json, "headline": "Invented headline"}
        elif failure == "verification":
            verification.evidence_json = {**verification.evidence_json, "status": "closed"}
        else:
            evidence = verified(
                container,
                status=VacancyStatus.CLOSED if failure == "closed" else VacancyStatus.OPEN,
                label=EligibilityLabel.CURRENTLY_INELIGIBLE
                if failure == "ineligible"
                else EligibilityLabel.CONFIRMED,
                review=failure != "unknown",
            )
            if failure == "deadline":
                evidence = evidence.model_copy(
                    update={"deadline_at": container.clock.now() - timedelta(hours=1)}
                )
            container.vacancies.record(
                session,
                job_id=ready["job"],
                profile_id=ready["profile"],
                evidence=evidence,
                actor="Demo Candidate",
                reviewed_by="Demo Candidate",
            )
            packet = container.packets.prepare(
                session, ready["app"], PacketSpec(evidence_keys=("python",)), actor="test"
            )
        with pytest.raises(ValueError):
            container.packets.request_approval(session, packet.id, actor="test")
        assert session.scalar(select(func.count(ActionRequest.id))) == 0


def test_approval_rechecks_material_changes_between_request_and_review(container, ready):
    with container.database.session() as session:
        action = container.packets.request_approval(session, ready["packet"], actor="test")
        action_id = action.id
    with container.database.session() as session:
        container.packets.import_profile(
            session, ready["profile"], snapshot("Changed profile"), reviewer="Demo Candidate"
        )
        with pytest.raises(ValueError, match="profile changed"):
            container.approvals.approve(session, action_id, reviewer="Demo Candidate")
        assert session.get(ActionRequest, action_id).status == ActionStatus.PENDING


def test_unknown_sponsorship_and_explicit_ineligibility_remain_mandatory(container, ready):
    with container.database.session() as session:
        container.vacancies.record(
            session,
            job_id=ready["job"],
            profile_id=ready["profile"],
            evidence=verified(container, label=EligibilityLabel.CURRENTLY_INELIGIBLE),
            actor="Demo Candidate",
            reviewed_by="Demo Candidate",
        )
        queue = container.opportunities.queue(session, profile_id=ready["profile"])
        card = next(item for item in queue.mandatory_user_review if item.job_id == ready["job"])
        assert card.profile_fit_score == 0.9
        assert card.eligibility_label == EligibilityLabel.CURRENTLY_INELIGIBLE
        assert session.get(Application, ready["app"]).status == ApplicationStatus.DRAFT


def test_deadline_and_snooze_retire_old_tasks_and_completed_tasks_stay_done(container, ready):
    with container.database.session() as session:
        for offset in (0, 1):
            container.applications.schedule(
                session,
                ready["app"],
                action="Review packet",
                due_at=container.clock.now() + timedelta(days=offset),
                deadline=container.clock.now() + timedelta(days=1),
                actor="Demo Candidate",
            )
        current = session.scalars(
            select(Task).where(
                Task.source_agent == "application-workflow", Task.status == TaskStatus.OPEN
            )
        ).all()
        assert len(current) == 2
        for task in current:
            task.status = TaskStatus.DONE
        container.applications.reconcile(session)
        assert (
            session.scalar(
                select(func.count(Task.id)).where(
                    Task.source_agent == "application-workflow", Task.status == TaskStatus.OPEN
                )
            )
            == 0
        )


def test_stage_outcomes_preserve_observed_feedback_and_funnel_counts(container, ready):
    submitted(container, ready)
    with container.database.session() as session:
        for stage in (ApplicationStatus.SCREENING, ApplicationStatus.INTERVIEWING):
            container.applications.stage(
                session,
                ready["app"],
                status=stage,
                evidence="Employer invitation",
                actor="Demo Candidate",
            )
        container.applications.stage(
            session,
            ready["app"],
            status=ApplicationStatus.REJECTED,
            evidence="Employer: position filled",
            reflection="Improve examples",
            actor="Demo Candidate",
        )
        app = session.get(Application, ready["app"])
        assert app.outcome_reason == "Employer: position filled"
        assert session.get(ApplicationWorkspace, ready["app"]).next_action is None
        report = container.applications.funnel(session, profile_id=ready["profile"])
        group = report["groups"][0]
        assert group["submitted"] == 1 and group["pending"] == 0
        assert group["recorded_stage_reached"] == {"screening": 1, "interviewing": 1, "offer": 0}
        assert (
            container.applications.funnel(
                session,
                profile_id=ready["profile"],
                since=container.clock.now() + timedelta(days=1),
            )["groups"]
            == []
        )
        with pytest.raises(ValueError, match="Invalid stage"):
            container.applications.stage(
                session,
                app.id,
                status=ApplicationStatus.OFFER,
                evidence="Ambiguous",
                actor="Demo Candidate",
            )


def test_external_rejection_reconciles_local_tasks(container, ready):
    submitted(container, ready)
    with container.database.session() as session:
        app = session.get(Application, ready["app"])
        app.status = ApplicationStatus.REJECTED
        container.planner.reconcile(session, profile_id=ready["profile"], actor="test")
        assert session.get(ApplicationWorkspace, app.id).next_action is None
        tasks = session.scalars(
            select(Task)
            .join(ApplicationTask)
            .where(
                ApplicationTask.application_id == app.id,
                Task.source_agent == "application-workflow",
            )
        ).all()
        assert all(task.status == TaskStatus.CANCELLED for task in tasks)


def test_interview_preparation_reschedule_conflict_and_debrief(container, ready):
    submitted(container, ready)
    plan = InterviewPlan(
        format="Technical screen",
        logistics="Video link in employer invitation",
        evidence_keys=("project",),
        practice_questions=("Explain your validation method",),
        questions_to_ask=("What will I own?",),
    )
    now = container.clock.now()
    with container.database.session() as session:
        meeting = container.interviews.schedule(
            session,
            ready["app"],
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
            timezone="Europe/London",
            plan=plan,
            actor="Demo Candidate",
        )
        assert session.get(Application, ready["app"]).status == ApplicationStatus.INTERVIEWING
        prep = session.get(InterviewWorkspace, meeting.id)
        assert "auditable research simulator" in prep.preparation_markdown
        container.planner.reconcile(session, profile_id=ready["profile"], actor="test")
        with pytest.raises(ValueError, match="overlaps"):
            container.interviews.schedule(
                session,
                ready["app"],
                starts_at=now + timedelta(days=1),
                ends_at=now + timedelta(days=1, hours=2),
                timezone="Europe/London",
                plan=plan,
                actor="Demo Candidate",
            )
        debrief = InterviewDebrief(
            employer_feedback="Next round will be confirmed",
            reflection="Explain assumptions more clearly",
            next_step="Review notes",
        )
        with pytest.raises(ValueError, match="before"):
            container.interviews.debrief(session, meeting.id, debrief, actor="Demo Candidate")
        container.interviews.reschedule(
            session,
            meeting.id,
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            timezone="Europe/London",
            actor="Demo Candidate",
        )
        prep_task = session.scalar(
            select(Task).where(Task.deduplication_key == f"interview-prep:{meeting.id}")
        )
        assert utc(prep_task.due_at) == now - timedelta(hours=14)
        container.interviews.debrief(session, meeting.id, debrief, actor="Demo Candidate")
        container.interviews.debrief(session, meeting.id, debrief, actor="Demo Candidate")
        assert meeting.status == MeetingStatus.COMPLETED and prep_task.status == TaskStatus.DONE
        assert prep.debrief_json["employer_feedback"] != prep.debrief_json["reflection"]
        assert session.get(ApplicationWorkspace, ready["app"]).next_action == "Review notes"
        with pytest.raises(ValueError, match="already recorded"):
            container.interviews.debrief(
                session,
                meeting.id,
                debrief.model_copy(update={"reflection": "changed"}),
                actor="Demo Candidate",
            )


@pytest.mark.parametrize(
    "invalid", ["receipt", "future", "unknown_fact", "reviewer", "naive", "stage"]
)
def test_invalid_operator_inputs_do_not_invent_state(container, ready, invalid):
    with pytest.raises((ValueError, LookupError)), container.database.session() as session:
        if invalid == "receipt":
            container.applications.submit_record(
                session,
                ready["app"],
                submitted_at=container.clock.now(),
                receipt=" ",
                actor="Demo Candidate",
            )
        elif invalid == "future":
            container.applications.submit_record(
                session,
                ready["app"],
                submitted_at=container.clock.now() + timedelta(days=1),
                receipt="ref",
                actor="Demo Candidate",
            )
        elif invalid == "unknown_fact":
            container.packets.prepare(
                session,
                ready["app"],
                PacketSpec(evidence_keys=("invented",)),
                actor="Demo Candidate",
            )
        elif invalid == "reviewer":
            container.packets.import_profile(session, ready["profile"], snapshot(), reviewer=" ")
        elif invalid == "naive":
            instant("2026-10-01T12:00:00")
        else:
            container.applications.stage(
                session,
                ready["app"],
                status=ApplicationStatus.APPLIED,
                evidence="none",
                actor="Demo Candidate",
            )
