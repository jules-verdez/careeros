"""Deterministic next-action policy.

This is the core answer to "what should I do next?". It derives a work queue
from persisted state and creates idempotent tasks; an LLM may explain the queue
but does not control it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import (
    ApplicationStatus,
    Direction,
    EmailStatus,
    JobStatus,
    MeetingStatus,
    TaskStatus,
)
from careeros.database.models import (
    Application,
    ApplicationTask,
    ApplicationWorkspace,
    Company,
    CompanyTask,
    Email,
    Job,
    JobTask,
    Meeting,
    PersonEmployment,
    ResearchReport,
    Task,
)
from careeros.services.applications import ApplicationService
from careeros.services.assessment import AssessmentService, RankedJob
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload, serialize_instant
from careeros.services.opportunities import OpportunityService


class NextActionPlanner:
    def __init__(
        self, clock: Clock, audit: AuditService, applications: ApplicationService | None = None
    ) -> None:
        self._clock = clock
        self._audit = audit
        self._applications = applications or ApplicationService(clock, audit)

    def reconcile(self, session: Session, *, profile_id: str, actor: str) -> list[Task]:
        created: list[Task] = []
        self._applications.reconcile(session)
        now = self._clock.now()
        today = now.date().isoformat()

        total_jobs = session.scalar(select(func.count(Job.id))) or 0
        created.extend(
            self._maybe_task(
                session,
                title="Discover new roles",
                description="Run all configured job-source connectors and inspect new records.",
                priority=65 if total_jobs else 100,
                estimated_minutes=10,
                deduplication_key=f"discover:{today}",
                actor=actor,
            )
        )

        latest = AssessmentService.latest(session, profile_id=profile_id)
        ranked_rows = [row for row in latest if row.job.status == JobStatus.DISCOVERED]
        current_keys = {self._review_key(row, profile_id) for row in ranked_rows}
        self._retire_obsolete_reviews(
            session,
            profile_id=profile_id,
            job_ids={row.job.id for row in latest},
            current_keys=current_keys,
            actor=actor,
        )
        for row in ranked_rows[:10]:
            job, assessment, company = row.job, row.assessment, row.company
            card = OpportunityService.describe(row)
            priority = round(55 + 40 * row.priority_score)
            tasks = self._maybe_task(
                session,
                title=f"Review role: {company.canonical_name} — {job.title}",
                description=(
                    f"Professional fit={card.profile_fit_score}, "
                    f"confidence={assessment.confidence:g}. "
                    f"Eligibility={card.eligibility_label.value}. "
                    f"{card.next_verification_step} "
                    "Review does not authorize shortlisting, applications, or outreach."
                ),
                priority=priority,
                estimated_minutes=5,
                deduplication_key=self._review_key(row, profile_id),
                actor=actor,
                job_id=job.id,
            )
            created.extend(tasks)

        shortlisted = session.execute(
            select(Job, Company)
            .join(Company, Company.id == Job.company_id)
            .where(
                Job.status == JobStatus.SHORTLISTED,
                ~exists().where(ResearchReport.company_id == Job.company_id),
            )
        ).all()
        for job, company in shortlisted:
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Research {company.canonical_name}",
                    description=(
                        "Create a cited markdown and JSON snapshot before outreach "
                        f"for {job.title}."
                    ),
                    priority=85,
                    estimated_minutes=20,
                    deduplication_key=f"research-company:{company.id}",
                    actor=actor,
                    company_id=company.id,
                )
            )

        researched_without_people = session.execute(
            select(Company).where(
                exists().where(ResearchReport.company_id == Company.id),
                ~exists().where(
                    PersonEmployment.company_id == Company.id,
                    PersonEmployment.is_current.is_(True),
                ),
                exists().where(
                    Job.company_id == Company.id,
                    Job.status == JobStatus.SHORTLISTED,
                ),
            )
        ).scalars()
        for company in researched_without_people:
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Identify decision-makers at {company.canonical_name}",
                    description=(
                        "Find public professional evidence and estimate hiring influence. "
                        "Record email only when a public source URL exists."
                    ),
                    priority=82,
                    estimated_minutes=15,
                    deduplication_key=f"decision-makers:{company.id}",
                    actor=actor,
                    company_id=company.id,
                )
            )

        due_applications = session.execute(
            select(Application, Job, Company)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
            .where(
                Application.next_followup_at.is_not(None),
                Application.next_followup_at <= now,
                ~exists().where(ApplicationWorkspace.application_id == Application.id),
                Application.status.in_(
                    [
                        ApplicationStatus.APPLIED,
                        ApplicationStatus.SCREENING,
                        ApplicationStatus.INTERVIEWING,
                    ]
                ),
            )
        ).all()
        for application, job, company in due_applications:
            due_key = application.next_followup_at.date().isoformat()
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Follow up: {company.canonical_name} — {job.title}",
                    description=(
                        "Review the thread and stage a concise, evidence-consistent follow-up."
                    ),
                    priority=92,
                    estimated_minutes=8,
                    deduplication_key=f"followup:{application.id}:{due_key}",
                    actor=actor,
                    due_at=application.next_followup_at,
                    application_id=application.id,
                )
            )

        horizon = now + timedelta(days=7)
        meetings = session.scalars(
            select(Meeting).where(
                Meeting.starts_at >= now,
                Meeting.starts_at <= horizon,
                Meeting.status.in_([MeetingStatus.TENTATIVE, MeetingStatus.CONFIRMED]),
            )
        )
        for meeting in meetings:
            application_id = meeting.application_id
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Prepare for interview: {meeting.title}",
                    description=(
                        "Build role-specific evidence, questions, and a concise interview plan."
                    ),
                    priority=98,
                    estimated_minutes=45,
                    deduplication_key=f"interview-prep:{meeting.id}",
                    actor=actor,
                    due_at=meeting.starts_at - timedelta(hours=12),
                    application_id=application_id,
                )
            )

        unanswered = session.scalars(
            select(Email).where(
                Email.direction == Direction.INBOUND,
                Email.status == EmailStatus.RECEIVED,
                Email.sent_or_received_at >= now - timedelta(days=2),
                ~exists().where(Email.in_reply_to_email_id == Email.id),
            )
        )
        for email in unanswered:
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Review reply: {email.subject}",
                    description="Classify the message and stage a reply if action is required.",
                    priority=94,
                    estimated_minutes=7,
                    deduplication_key=f"review-inbound-email:{email.id}",
                    actor=actor,
                    application_id=email.application_id,
                )
            )

        closed_without_outcome = session.execute(
            select(Application, Job, Company)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
            .where(
                Application.status.in_(
                    [
                        ApplicationStatus.OFFER,
                        ApplicationStatus.REJECTED,
                        ApplicationStatus.WITHDRAWN,
                        ApplicationStatus.CLOSED,
                    ]
                ),
                Application.outcome_reason.is_(None),
            )
        ).all()
        for application, job, company in closed_without_outcome:
            created.extend(
                self._maybe_task(
                    session,
                    title=f"Record learning: {company.canonical_name} — {job.title}",
                    description=(
                        "Capture the outcome, stage reached, signal quality, "
                        "and one process change."
                    ),
                    priority=60,
                    estimated_minutes=10,
                    deduplication_key=f"learn-application:{application.id}",
                    actor=actor,
                    application_id=application.id,
                )
            )
        return created

    @staticmethod
    def _review_key(row: RankedJob, profile_id: str) -> str:
        assessment = row.assessment
        revision = hash_payload(
            {
                "assessment_id": assessment.id,
                "assessed_at": serialize_instant(assessment.assessed_at),
                "fit": assessment.fit_score,
                "confidence": assessment.confidence,
                "competition": assessment.competition_score,
                "explanation": assessment.explanation_json,
            }
        )
        return f"review-job:{profile_id}:{row.job.id}:{revision}"

    def _retire_obsolete_reviews(
        self,
        session: Session,
        *,
        profile_id: str,
        job_ids: set[str],
        current_keys: set[str],
        actor: str,
    ) -> None:
        tasks = session.execute(
            select(Task, JobTask.job_id)
            .join(JobTask, JobTask.task_id == Task.id)
            .where(
                Task.source_agent == "daily-agent-v1",
                Task.deduplication_key.startswith("review-job:"),
                Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
            )
        ).all()
        for task, job_id in tasks:
            key = task.deduplication_key or ""
            owned = key.startswith(f"review-job:{profile_id}:") or (
                job_id in job_ids and key.startswith(f"review-job:{job_id}:")
            )
            if not owned or key in current_keys:
                continue
            task.status = TaskStatus.CANCELLED
            self._audit.append(
                session,
                actor=actor,
                event_type="task.cancelled",
                aggregate_type="task",
                aggregate_id=task.id,
                payload={"reason": "role status or current assessment changed", "job_id": job_id},
            )

    def _maybe_task(
        self,
        session: Session,
        *,
        title: str,
        description: str,
        priority: int,
        estimated_minutes: int,
        deduplication_key: str,
        actor: str,
        due_at: object | None = None,
        company_id: str | None = None,
        job_id: str | None = None,
        application_id: str | None = None,
    ) -> list[Task]:
        existing = session.scalar(select(Task).where(Task.deduplication_key == deduplication_key))
        if existing is not None:
            return []
        task = Task(
            title=title,
            description=description,
            status=TaskStatus.OPEN,
            priority=priority,
            due_at=due_at,
            estimated_minutes=estimated_minutes,
            source_agent="daily-agent-v1",
            deduplication_key=deduplication_key,
        )
        session.add(task)
        session.flush()
        if company_id:
            session.add(CompanyTask(company_id=company_id, task_id=task.id))
        if job_id:
            session.add(JobTask(job_id=job_id, task_id=task.id))
        if application_id:
            session.add(ApplicationTask(application_id=application_id, task_id=task.id))
        self._audit.append(
            session,
            actor=actor,
            event_type="task.created",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={
                "priority": priority,
                "deduplication_key": deduplication_key,
                "source_agent": task.source_agent,
            },
        )
        return [task]
