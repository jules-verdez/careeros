"""Morning brief generation from the deterministic work queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import (
    ActionStatus,
    ApplicationStatus,
    Direction,
    EmailStatus,
    JobStatus,
    MeetingStatus,
    TaskStatus,
)
from careeros.database.models import (
    ActionRequest,
    Application,
    ApplicationTask,
    CandidateProfile,
    Company,
    CompanyTask,
    DailyBrief,
    DailyBriefItem,
    Email,
    Job,
    JobTask,
    Meeting,
    Recruiter,
    Task,
)
from careeros.services.audit import AuditService
from careeros.services.opportunities import OpportunityQueue, OpportunityService
from careeros.services.planning import NextActionPlanner


@dataclass(frozen=True)
class BriefView:
    brief: DailyBrief
    items: tuple[DailyBriefItem, ...]
    opportunity_queue: OpportunityQueue


class DailyBriefService:
    def __init__(
        self,
        clock: Clock,
        audit: AuditService,
        planner: NextActionPlanner,
        opportunities: OpportunityService,
        *,
        timezone: str,
    ) -> None:
        self._clock = clock
        self._audit = audit
        self._planner = planner
        self._opportunities = opportunities
        self._timezone = ZoneInfo(timezone)

    def generate(
        self,
        session: Session,
        *,
        profile: CandidateProfile,
        actor: str,
        limit: int = 3,
    ) -> BriefView:
        self._planner.reconcile(session, profile_id=profile.id, actor=actor)
        now = self._clock.now()
        local_now = self._local(now)
        open_tasks = self.next_tasks(session, limit=limit)
        previous_revision = session.scalar(
            select(func.max(DailyBrief.revision)).where(DailyBrief.brief_date == local_now.date())
        )
        revision = (previous_revision or 0) + 1
        counts = self.live_counts(session)
        opportunity_queue = self._opportunities.queue(session, profile_id=profile.id)
        counts["mandatory_review_count"] = len(opportunity_queue.mandatory_user_review)
        brief = DailyBrief(
            brief_date=local_now.date(),
            revision=revision,
            generated_at=now,
            candidate_profile_id=profile.id,
            summary_json=counts,
            estimated_minutes=sum(task.estimated_minutes or 0 for task in open_tasks),
        )
        session.add(brief)
        session.flush()
        items: list[DailyBriefItem] = []
        for rank, task in enumerate(open_tasks, start=1):
            company_name = self._company_for_task(session, task.id)
            roi_proxy = self._roi_proxy(task, now=now)
            rationale = (
                f"Priority {task.priority}/100"
                + (f"; linked to {company_name}" if company_name else "")
                + ("; due or overdue" if self._is_due(task, now=now) else "")
            )
            item = DailyBriefItem(
                daily_brief_id=brief.id,
                task_id=task.id,
                rank=rank,
                recommendation=task.title,
                rationale=rationale,
                expected_roi=roi_proxy,
                estimated_minutes=task.estimated_minutes or 0,
            )
            session.add(item)
            items.append(item)
        session.flush()
        self._audit.append(
            session,
            actor=actor,
            event_type="daily_brief.generated",
            aggregate_type="daily_brief",
            aggregate_id=brief.id,
            payload={
                "brief_date": brief.brief_date.isoformat(),
                "revision": revision,
                "task_ids": [task.id for task in open_tasks],
                "summary": counts,
            },
        )
        return BriefView(brief=brief, items=tuple(items), opportunity_queue=opportunity_queue)

    def next_tasks(self, session: Session, *, limit: int) -> list[Task]:
        """The open work a brief recommends now, highest priority first.

        Application-workflow tasks wait until they are due, except deadlines, which
        surface two days early so a submission is never prepared at the last minute.
        """
        now = self._clock.now()
        return list(
            session.scalars(
                select(Task)
                .where(Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]))
                .where(
                    or_(
                        Task.source_agent.is_(None),
                        Task.source_agent != "application-workflow",
                        Task.due_at <= now,
                        (Task.deduplication_key.like("application-deadline:%"))
                        & (Task.due_at <= now + timedelta(days=2)),
                    )
                )
                .order_by(Task.priority.desc(), Task.due_at.asc().nullslast(), Task.created_at)
                .limit(limit)
            ).all()
        )

    def live_counts(self, session: Session) -> dict[str, int]:
        """Current dashboard counts; "today" is the operator's local calendar day."""
        now = self._clock.now()
        start_local = datetime.combine(self._local(now).date(), time.min, self._timezone)
        return self._counts(
            session,
            now=now,
            start_utc=start_local.astimezone(UTC),
            end_utc=(start_local + timedelta(days=1)).astimezone(UTC),
        )

    def latest(self, session: Session, *, profile_id: str | None = None) -> BriefView | None:
        query = select(DailyBrief).order_by(
            DailyBrief.brief_date.desc(),
            DailyBrief.revision.desc(),
        )
        if profile_id is not None:
            query = query.where(DailyBrief.candidate_profile_id == profile_id)
        brief = session.scalar(query.limit(1))
        if brief is None:
            return None
        items = session.scalars(
            select(DailyBriefItem)
            .where(DailyBriefItem.daily_brief_id == brief.id)
            .order_by(DailyBriefItem.rank)
        ).all()
        return BriefView(
            brief=brief,
            items=tuple(items),
            opportunity_queue=self._opportunities.queue(
                session, profile_id=brief.candidate_profile_id
            ),
        )

    def _counts(
        self,
        session: Session,
        *,
        now: datetime,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, int]:
        upcoming_interviews = session.scalar(
            select(func.count(Meeting.id)).where(
                Meeting.starts_at >= now,
                Meeting.starts_at < now + timedelta(days=7),
                Meeting.status.in_([MeetingStatus.TENTATIVE, MeetingStatus.CONFIRMED]),
            )
        )
        new_jobs = session.scalar(
            select(func.count(Job.id)).where(
                Job.first_seen_at >= start_utc,
                Job.first_seen_at < end_utc,
            )
        )
        companies_hiring = session.scalar(
            select(func.count(distinct(Job.company_id))).where(
                Job.status.in_([JobStatus.DISCOVERED, JobStatus.SHORTLISTED])
            )
        )
        followups_due = session.scalar(
            select(func.count(Application.id)).where(
                Application.next_followup_at.is_not(None),
                Application.next_followup_at <= now,
                Application.status.in_(
                    [
                        ApplicationStatus.APPLIED,
                        ApplicationStatus.SCREENING,
                        ApplicationStatus.INTERVIEWING,
                    ]
                ),
            )
        )
        recruiter_replies = session.scalar(
            select(func.count(Email.id))
            .join(Recruiter, Recruiter.person_id == Email.person_id)
            .where(
                Email.direction == Direction.INBOUND,
                Email.status == EmailStatus.RECEIVED,
                Email.sent_or_received_at >= start_utc,
                Email.sent_or_received_at < end_utc,
            )
        )
        open_applications = session.scalar(
            select(func.count(Application.id)).where(
                Application.status.in_(
                    [
                        ApplicationStatus.APPLIED,
                        ApplicationStatus.SCREENING,
                        ApplicationStatus.INTERVIEWING,
                    ]
                )
            )
        )
        pending_approvals = session.scalar(
            select(func.count(ActionRequest.id)).where(ActionRequest.status == ActionStatus.PENDING)
        )
        draft_emails = session.scalar(
            select(func.count(Email.id)).where(Email.status == EmailStatus.DRAFT)
        )
        return {
            "upcoming_interviews": upcoming_interviews or 0,
            "new_jobs": new_jobs or 0,
            "companies_hiring": companies_hiring or 0,
            "followups_due": followups_due or 0,
            "recruiter_replies": recruiter_replies or 0,
            "open_applications": open_applications or 0,
            "pending_approvals": pending_approvals or 0,
            "draft_emails": draft_emails or 0,
        }

    @classmethod
    def _roi_proxy(cls, task: Task, *, now: datetime) -> float:
        urgency = 1.0 if cls._is_due(task, now=now) else 0.9
        return round(min(1.0, task.priority / 100 * urgency), 4)

    @staticmethod
    def _is_due(task: Task, *, now: datetime) -> bool:
        if task.due_at is None:
            return False
        # SQLite returns persisted UTC timestamps without timezone information.
        due = task.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return due <= now

    @staticmethod
    def _company_for_task(session: Session, task_id: str) -> str | None:
        direct = session.scalar(
            select(Company.canonical_name)
            .join(CompanyTask, CompanyTask.company_id == Company.id)
            .where(CompanyTask.task_id == task_id)
        )
        if direct:
            return direct
        via_job = session.scalar(
            select(Company.canonical_name)
            .join(Job, Job.company_id == Company.id)
            .join(JobTask, JobTask.job_id == Job.id)
            .where(JobTask.task_id == task_id)
        )
        if via_job:
            return via_job
        return session.scalar(
            select(Company.canonical_name)
            .join(Job, Job.company_id == Company.id)
            .join(Application, Application.job_id == Job.id)
            .join(ApplicationTask, ApplicationTask.application_id == Application.id)
            .where(ApplicationTask.task_id == task_id)
        )

    def _local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self._timezone)
