"""Interview logistics, preparation from real examples, and separate feedback/reflection."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.enums import ApplicationStatus, MeetingStatus, TaskStatus
from careeros.database.models import (
    Application,
    ApplicationTask,
    ApplicationWorkspace,
    InterviewWorkspace,
    Meeting,
    Task,
)
from careeros.domain.workflow import CandidateSnapshot, InterviewDebrief, InterviewPlan, utc
from careeros.services.applications import TERMINAL, ApplicationService, require
from careeros.services.packets import PacketService


class InterviewService:
    def __init__(self, applications: ApplicationService, packets: PacketService) -> None:
        self.applications = applications
        self.packets = packets

    def schedule(
        self,
        session: Session,
        application_id: str,
        *,
        starts_at: datetime,
        ends_at: datetime,
        timezone: str,
        plan: InterviewPlan,
        actor: str,
    ) -> Meeting:
        app = require(session, Application, application_id)
        workspace = require(session, ApplicationWorkspace, application_id)
        if app.status not in {
            ApplicationStatus.APPLIED,
            ApplicationStatus.SCREENING,
            ApplicationStatus.INTERVIEWING,
        }:
            raise ValueError("Interviews require an active submitted application")
        ZoneInfo(timezone)
        if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
            raise ValueError("Interview needs timezone-aware times and an end after its start")
        version = self.packets.latest_version(session, workspace.candidate_profile_id)
        if version is None:
            raise ValueError("Import candidate evidence before preparing an interview")
        from careeros.services.normalization import hash_payload

        if hash_payload(version.payload_json) != version.sha256:
            raise ValueError("Candidate version content changed")
        facts = {
            fact.key: fact for fact in CandidateSnapshot.model_validate(version.payload_json).facts
        }
        if set(plan.evidence_keys) - facts.keys():
            raise ValueError("Interview examples must refer to existing candidate evidence")
        overlaps = session.scalar(
            select(Meeting)
            .where(
                Meeting.starts_at < utc(ends_at),
                Meeting.ends_at > utc(starts_at),
                Meeting.status.in_([MeetingStatus.TENTATIVE, MeetingStatus.CONFIRMED]),
            )
            .limit(1)
        )
        if overlaps:
            raise ValueError(f"Interview overlaps an existing meeting: {overlaps.id}")
        preparation = [
            f"# {plan.format}",
            f"Interviewer: {plan.interviewer or 'Unknown'}",
            f"Logistics: {plan.logistics}",
            "## Examples from reviewed evidence",
        ]
        preparation.extend(
            f"- {facts[key].text}\n  Source: {facts[key].source}"
            for key in dict.fromkeys(plan.evidence_keys)
        )
        preparation.extend(
            [
                "## Practice questions (not predictions)",
                *[f"- {question}" for question in plan.practice_questions],
                "## Questions to ask",
                *[f"- {question}" for question in plan.questions_to_ask],
            ]
        )
        meeting = Meeting(
            application_id=app.id,
            title=plan.format,
            starts_at=utc(starts_at),
            ends_at=utc(ends_at),
            timezone=timezone,
            location=plan.logistics,
            status=MeetingStatus.CONFIRMED,
        )
        session.add(meeting)
        session.flush()
        session.add(
            InterviewWorkspace(
                meeting_id=meeting.id,
                candidate_version_id=version.id,
                plan_json=plan.model_dump(mode="json"),
                preparation_markdown="\n\n".join(preparation),
            )
        )
        self.applications.event(
            session,
            app.id,
            "interview_scheduled",
            {
                "meeting_id": meeting.id,
                "starts_at": utc(starts_at).isoformat(),
                "ends_at": utc(ends_at).isoformat(),
                "timezone": timezone,
            },
            actor,
        )
        if app.status != ApplicationStatus.INTERVIEWING:
            self.applications.stage(
                session,
                app.id,
                status=ApplicationStatus.INTERVIEWING,
                evidence=f"Human-recorded interview {meeting.id}",
                actor=actor,
            )
        return meeting

    def debrief(
        self, session: Session, meeting_id: str, debrief: InterviewDebrief, *, actor: str
    ) -> None:
        meeting = require(session, Meeting, meeting_id)
        workspace = require(session, InterviewWorkspace, meeting_id)
        if meeting.status == MeetingStatus.CANCELLED:
            raise ValueError("Cannot debrief a cancelled interview")
        if utc(meeting.starts_at) > self.applications.clock.now():
            raise ValueError("Cannot record a debrief before the interview starts")
        payload = debrief.model_dump(mode="json")
        if workspace.debrief_json == payload:
            return
        if workspace.debrief_json:
            raise ValueError("Debrief already recorded; preserve the original feedback")
        workspace.debrief_json = payload
        meeting.status = MeetingStatus.COMPLETED
        assert meeting.application_id is not None
        self.applications.event(
            session,
            meeting.application_id,
            "interview_debrief",
            {
                "meeting_id": meeting_id,
                **payload,
            },
            actor,
        )
        self._retire_prep(session, meeting)
        application = require(session, Application, meeting.application_id)
        if application.status not in TERMINAL:
            self.applications.schedule(
                session,
                meeting.application_id,
                action=debrief.next_step,
                due_at=self.applications.clock.now(),
                actor=actor,
            )

    def reschedule(
        self,
        session: Session,
        meeting_id: str,
        *,
        starts_at: datetime,
        ends_at: datetime,
        timezone: str,
        actor: str,
    ) -> Meeting:
        meeting = require(session, Meeting, meeting_id)
        require(session, InterviewWorkspace, meeting_id)
        if meeting.status != MeetingStatus.CONFIRMED:
            raise ValueError("Only a confirmed interview can be rescheduled")
        ZoneInfo(timezone)
        if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
            raise ValueError("Interview needs timezone-aware times and an end after its start")
        conflict = session.scalar(
            select(Meeting)
            .where(
                Meeting.id != meeting_id,
                Meeting.starts_at < utc(ends_at),
                Meeting.ends_at > utc(starts_at),
                Meeting.status.in_([MeetingStatus.TENTATIVE, MeetingStatus.CONFIRMED]),
            )
            .limit(1)
        )
        if conflict:
            raise ValueError(f"Interview overlaps an existing meeting: {conflict.id}")
        meeting.starts_at, meeting.ends_at, meeting.timezone = (
            utc(starts_at),
            utc(ends_at),
            timezone,
        )
        assert meeting.application_id is not None
        self.applications.event(
            session,
            meeting.application_id,
            "interview_rescheduled",
            {
                "meeting_id": meeting_id,
                "starts_at": utc(starts_at).isoformat(),
                "ends_at": utc(ends_at).isoformat(),
                "timezone": timezone,
            },
            actor,
        )
        from datetime import timedelta

        for task in session.scalars(
            select(Task).where(
                Task.deduplication_key == f"interview-prep:{meeting_id}",
                Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]),
            )
        ):
            task.due_at = utc(starts_at) - timedelta(hours=12)
        return meeting

    def cancel(self, session: Session, meeting_id: str, *, reason: str, actor: str) -> None:
        meeting = require(session, Meeting, meeting_id)
        if not reason.strip() or meeting.status == MeetingStatus.COMPLETED:
            raise ValueError("Cancellation requires a reason and an uncompleted interview")
        meeting.status = MeetingStatus.CANCELLED
        assert meeting.application_id is not None
        self.applications.event(
            session,
            meeting.application_id,
            "interview_cancelled",
            {
                "meeting_id": meeting_id,
                "reason": reason,
            },
            actor,
        )
        self._retire_prep(session, meeting)

    @staticmethod
    def _retire_prep(session: Session, meeting: Meeting) -> None:
        tasks = session.scalars(
            select(Task)
            .join(ApplicationTask)
            .where(
                ApplicationTask.application_id == meeting.application_id,
                Task.deduplication_key == f"interview-prep:{meeting.id}",
                Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS]),
            )
        )
        for task in tasks:
            task.status = (
                TaskStatus.DONE
                if meeting.status == MeetingStatus.COMPLETED
                else TaskStatus.CANCELLED
            )
