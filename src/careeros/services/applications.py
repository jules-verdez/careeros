"""Local application tracking, explicit next actions, and evidence-led outcomes."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import ApplicationStatus, JobStatus, MeetingStatus, TaskStatus
from careeros.database.models import (
    Application,
    ApplicationEvent,
    ApplicationPacket,
    ApplicationSubmission,
    ApplicationTask,
    ApplicationWorkspace,
    CandidateProfile,
    Company,
    Job,
    JobObservation,
    Meeting,
    Task,
)
from careeros.domain.workflow import utc
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload

TERMINAL = {ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.CLOSED}
PROGRESS = {
    ApplicationStatus.DRAFT: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.SCREENING: 2,
    ApplicationStatus.INTERVIEWING: 3,
    ApplicationStatus.OFFER: 4,
}


def require[T](session: Session, model: type[T], identity: str) -> T:
    value = session.get(model, identity)
    if value is None:
        raise LookupError(f"{model.__name__} not found: {identity}")
    return value


class ApplicationService:
    def __init__(self, clock: Clock, audit: AuditService) -> None:
        self.clock = clock
        self.audit = audit

    def event(
        self, session: Session, application_id: str, kind: str, payload: dict[str, Any], actor: str
    ) -> ApplicationEvent:
        event = ApplicationEvent(
            application_id=application_id,
            position=(
                session.scalar(
                    select(func.max(ApplicationEvent.position)).where(
                        ApplicationEvent.application_id == application_id
                    )
                )
                or 0
            )
            + 1,
            kind=kind,
            occurred_at=self.clock.now(),
            actor=actor,
            payload_json=payload,
        )
        session.add(event)
        session.flush()
        self.audit.append(
            session,
            actor=actor,
            event_type=f"application.{kind}",
            aggregate_type="application",
            aggregate_id=application_id,
            payload={"event_id": event.id, **payload},
        )
        return event

    def start(
        self,
        session: Session,
        *,
        job_id: str,
        profile_id: str,
        actor: str,
        role_family: str = "unclassified",
        source_label: str = "unclassified",
    ) -> Application:
        require(session, Job, job_id)
        require(session, CandidateProfile, profile_id)
        application = session.scalar(select(Application).where(Application.job_id == job_id))
        if application is None:
            urls = select(JobObservation.source_url).where(JobObservation.job_id == job_id)
            duplicate = session.scalar(
                select(Application)
                .join(JobObservation, JobObservation.job_id == Application.job_id)
                .where(JobObservation.source_url.in_(urls))
                .limit(1)
            )
            if duplicate:
                raise ValueError(
                    f"This vacancy URL is already tracked by application {duplicate.id}"
                )
            application = Application(job_id=job_id, status=ApplicationStatus.DRAFT)
            session.add(application)
            session.flush()
        workspace = session.get(ApplicationWorkspace, application.id)
        if workspace:
            if workspace.candidate_profile_id != profile_id:
                raise ValueError("Application belongs to a different candidate profile")
            return application
        session.add(
            ApplicationWorkspace(
                application_id=application.id,
                candidate_profile_id=profile_id,
                next_action="Review application and choose the next step",
                next_action_at=self.clock.now(),
                role_family=role_family.strip() or "unclassified",
                source_label=source_label.strip() or "unclassified",
            )
        )
        self.event(session, application.id, "tracked", {"status": application.status.value}, actor)
        self.reconcile(session)
        return application

    def schedule(
        self,
        session: Session,
        application_id: str,
        *,
        action: str,
        due_at: datetime,
        actor: str,
        deadline: datetime | None = None,
        followup: bool = False,
    ) -> None:
        app = require(session, Application, application_id)
        workspace = require(session, ApplicationWorkspace, application_id)
        if app.status in TERMINAL:
            raise ValueError("Closed applications cannot have active next actions")
        if not action.strip():
            raise ValueError("Next action must not be blank")
        if due_at.tzinfo is None or (deadline is not None and deadline.tzinfo is None):
            raise ValueError("Action dates must include timezone offsets")
        if followup and app.status == ApplicationStatus.DRAFT:
            raise ValueError("Record a submission before scheduling an application follow-up")
        workspace.next_action = action.strip()
        workspace.next_action_at = utc(due_at)
        if deadline is not None:
            workspace.deadline_at = utc(deadline)
        app.next_followup_at = utc(due_at) if followup else None
        self.event(
            session,
            application_id,
            "next_action",
            {
                "action": action.strip(),
                "due_at": utc(due_at).isoformat(),
                "deadline_at": utc(workspace.deadline_at).isoformat()
                if workspace.deadline_at
                else None,
                "followup": followup,
            },
            actor,
        )
        self.reconcile(session)

    def submit_record(
        self,
        session: Session,
        application_id: str,
        *,
        submitted_at: datetime,
        receipt: str,
        actor: str,
        packet_id: str | None = None,
    ) -> ApplicationSubmission:
        app = require(session, Application, application_id)
        workspace = require(session, ApplicationWorkspace, application_id)
        if not receipt.strip():
            raise ValueError("Submission evidence or receipt reference is required")
        if submitted_at.tzinfo is None or utc(submitted_at) > self.clock.now():
            raise ValueError("Submission time must be timezone-aware and cannot be in the future")
        if packet_id:
            packet = require(session, ApplicationPacket, packet_id)
            if (
                packet.application_id != application_id
                or hash_payload(packet.payload_json) != packet.sha256
            ):
                raise ValueError("Packet belongs to another application or its content changed")
        existing = session.scalar(
            select(ApplicationSubmission).where(
                ApplicationSubmission.application_id == application_id
            )
        )
        if existing:
            if (
                existing.receipt == receipt.strip()
                and existing.packet_id == packet_id
                and utc(existing.submitted_at) == utc(submitted_at)
            ):
                return existing
            raise ValueError("Submission already recorded; duplicate submission evidence refused")
        if app.status in TERMINAL:
            raise ValueError("Cannot submit a closed application")
        submission = ApplicationSubmission(
            application_id=application_id,
            packet_id=packet_id,
            submitted_at=utc(submitted_at),
            receipt=receipt.strip(),
            recorded_by=actor,
            recorded_at=self.clock.now(),
        )
        session.add(submission)
        app.applied_at = utc(submitted_at)
        if app.status == ApplicationStatus.DRAFT:
            app.status = ApplicationStatus.APPLIED
        require(session, Job, app.job_id).status = JobStatus.APPLIED
        workspace.next_action = "Review response and choose a follow-up date"
        workspace.next_action_at = self.clock.now()
        workspace.deadline_at = None
        self.event(
            session,
            application_id,
            "submission_recorded",
            {
                "submitted_at": utc(submitted_at).isoformat(),
                "receipt": receipt.strip(),
                "packet_id": packet_id,
                "source": "human_reported",
                "status": app.status.value,
            },
            actor,
        )
        self.reconcile(session)
        return submission

    def stage(
        self,
        session: Session,
        application_id: str,
        *,
        status: ApplicationStatus,
        evidence: str,
        actor: str,
        reflection: str = "",
    ) -> Application:
        app = require(session, Application, application_id)
        workspace = require(session, ApplicationWorkspace, application_id)
        if not evidence.strip():
            raise ValueError("Record the evidence for a stage change")
        if status == app.status:
            return app
        if status in {ApplicationStatus.DRAFT, ApplicationStatus.APPLIED}:
            raise ValueError("Use record-submission to establish applied status")
        if app.status in TERMINAL or (
            status not in TERMINAL
            and (app.applied_at is None or PROGRESS[status] <= PROGRESS.get(app.status, -1))
        ):
            raise ValueError(f"Invalid stage transition: {app.status.value} -> {status.value}")
        previous = app.status
        app.status = status
        app.next_followup_at = None
        if status in TERMINAL:
            app.closed_at = self.clock.now()
            app.outcome_reason = evidence.strip()
            workspace.next_action = workspace.next_action_at = None
            require(session, Job, app.job_id).status = JobStatus.CLOSED
        else:
            workspace.next_action = f"Plan next step for {status.value}"
            workspace.next_action_at = self.clock.now()
        self.event(
            session,
            application_id,
            "stage_changed",
            {
                "previous_status": previous.value,
                "status": status.value,
                "employer_evidence": evidence.strip(),
                "reflection": reflection.strip(),
            },
            actor,
        )
        self.reconcile(session)
        return app

    def reconcile(self, session: Session) -> None:
        """Keep current application tasks in sync, including externally recorded rejections."""
        rows = session.execute(
            select(Application, ApplicationWorkspace, Job, Company)
            .join(ApplicationWorkspace, ApplicationWorkspace.application_id == Application.id)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
        ).all()
        for app, workspace, job, company in rows:
            wanted: dict[str, tuple[str, datetime, int]] = {}
            if app.status in TERMINAL:
                workspace.next_action = None
                workspace.next_action_at = None
                workspace.deadline_at = None
                app.next_followup_at = None
                for meeting in session.scalars(
                    select(Meeting).where(
                        Meeting.application_id == app.id,
                        Meeting.status.in_([MeetingStatus.TENTATIVE, MeetingStatus.CONFIRMED]),
                        Meeting.starts_at >= self.clock.now(),
                    )
                ):
                    meeting.status = MeetingStatus.CANCELLED
                    self.event(
                        session,
                        app.id,
                        "interview_cancelled",
                        {
                            "meeting_id": meeting.id,
                            "reason": "Application closed",
                        },
                        "application-workflow",
                    )
            if app.status not in TERMINAL:
                if workspace.next_action and workspace.next_action_at:
                    title = (
                        f"Follow up: {company.canonical_name} — {job.title}"
                        if app.next_followup_at
                        else f"{workspace.next_action}: {company.canonical_name} — {job.title}"
                    )
                    key = hash_payload(
                        [workspace.next_action, utc(workspace.next_action_at).isoformat()]
                    )
                    wanted[f"application-next:{app.id}:{key}"] = (
                        title,
                        workspace.next_action_at,
                        94,
                    )
                if workspace.deadline_at and app.status == ApplicationStatus.DRAFT:
                    key = utc(workspace.deadline_at).isoformat()
                    wanted[f"application-deadline:{app.id}:{key}"] = (
                        f"Application deadline: {company.canonical_name} — {job.title}",
                        workspace.deadline_at,
                        96,
                    )
            tasks = session.scalars(
                select(Task).join(ApplicationTask).where(ApplicationTask.application_id == app.id)
            ).all()
            for task in tasks:
                key = task.deduplication_key or ""
                managed = key.startswith(
                    ("application-next:", "application-deadline:", "followup:")
                )
                managed = managed or (app.status in TERMINAL and key.startswith("interview-prep:"))
                if (
                    managed
                    and key not in wanted
                    and task.status in {TaskStatus.OPEN, TaskStatus.IN_PROGRESS}
                ):
                    task.status = TaskStatus.CANCELLED
                    self.audit.append(
                        session,
                        actor="application-workflow",
                        event_type="task.cancelled",
                        aggregate_type="task",
                        aggregate_id=task.id,
                        payload={"application_id": app.id},
                    )
            existing = {task.deduplication_key for task in tasks}
            for key, (title, due, priority) in wanted.items():
                if key in existing:
                    continue
                task = Task(
                    title=title,
                    description=workspace.next_action,
                    due_at=due,
                    priority=priority,
                    estimated_minutes=10,
                    status=TaskStatus.OPEN,
                    source_agent="application-workflow",
                    deduplication_key=key,
                )
                session.add(task)
                session.flush()
                session.add(ApplicationTask(application_id=app.id, task_id=task.id))
                self.audit.append(
                    session,
                    actor="application-workflow",
                    event_type="task.created",
                    aggregate_type="task",
                    aggregate_id=task.id,
                    payload={"application_id": app.id, "key": key},
                )
        session.flush()

    def funnel(
        self, session: Session, *, profile_id: str, since: datetime | None = None
    ) -> dict[str, Any]:
        rows = session.execute(
            select(Application, ApplicationWorkspace)
            .join(ApplicationWorkspace, ApplicationWorkspace.application_id == Application.id)
            .where(ApplicationWorkspace.candidate_profile_id == profile_id)
        ).all()
        groups: dict[tuple[str, str], list[Application]] = defaultdict(list)
        for app, workspace in rows:
            if app.applied_at and (since is None or utc(app.applied_at) >= utc(since)):
                groups[(workspace.role_family, workspace.source_label)].append(app)
        output = []
        for (family, source), apps in sorted(groups.items()):
            counts = Counter(app.status.value for app in apps)
            ids = {app.id for app in apps}
            events = session.scalars(
                select(ApplicationEvent).where(ApplicationEvent.application_id.in_(ids))
            ).all()
            reached: dict[str, set[str]] = defaultdict(set)
            for event in events:
                status = event.payload_json.get("status")
                if status in {"screening", "interviewing", "offer"}:
                    reached[status].add(event.application_id)
            for app in apps:
                if app.status.value in {"screening", "interviewing", "offer"}:
                    reached[app.status.value].add(app.id)
            output.append(
                {
                    "role_family": family,
                    "source": source,
                    "submitted": len(apps),
                    "current_stages": dict(counts),
                    "recorded_stage_reached": {
                        stage: len(reached[stage])
                        for stage in ("screening", "interviewing", "offer")
                    },
                    "pending": sum(
                        app.status not in TERMINAL and app.status != ApplicationStatus.OFFER
                        for app in apps
                    ),
                    "days_since_submission": {
                        app.id: (self.clock.now() - utc(app.applied_at)).days
                        for app in apps
                        if app.applied_at
                    },
                }
            )
        return {
            "as_of": self.clock.now().isoformat(),
            "since": since.isoformat() if since else None,
            "groups": output,
            "note": "Counts, not hiring probabilities. Only recorded stages "
            "are counted; missing historical stages are unknown. "
            "Pending applications remain visible.",
        }
