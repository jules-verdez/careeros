"""Stable values persisted by CareerOS."""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class ActionKind(StrEnum):
    CREATE_EMAIL_DRAFT = "create_email_draft"
    SEND_EMAIL = "send_email"
    SEND_LINKEDIN = "send_linkedin"
    SUBMIT_APPLICATION = "submit_application"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    MODIFY_EXTERNAL_RECORD = "modify_external_record"


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConnectorRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ContactDeliveryStatus(StrEnum):
    UNKNOWN = "unknown"
    DELIVERABLE = "deliverable"
    RISKY = "risky"
    INVALID = "invalid"


class ContactEnrichmentReason(StrEnum):
    HUNTER_MISSING = "hunter_missing"
    HUNTER_ACCEPT_ALL = "hunter_accept_all"
    HUNTER_LOW_CONFIDENCE = "hunter_low_confidence"


class DeliveryEventKind(StrEnum):
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    DELAYED = "delayed"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class DocumentKind(StrEnum):
    CV = "cv"
    JOB_DESCRIPTION = "job_description"
    COMPANY_RESEARCH_MARKDOWN = "company_research_markdown"
    COMPANY_RESEARCH_JSON = "company_research_json"
    EMAIL = "email"
    INTERVIEW_PREP = "interview_prep"
    ANNUAL_REPORT = "annual_report"
    ARTICLE = "article"
    OTHER = "other"


class EmailStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENT = "sent"
    RECEIVED = "received"
    FAILED = "failed"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    OTHER = "other"


class InteractionKind(StrEnum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    CALL = "call"
    MEETING = "meeting"
    APPLICATION = "application"
    INTERVIEW = "interview"
    NOTE = "note"


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    SHORTLISTED = "shortlisted"
    DISMISSED = "dismissed"
    APPLIED = "applied"
    CLOSED = "closed"
    # Historical imports already persist this terminal state in SQLite.
    ARCHIVED = "archived"


class MeetingStatus(StrEnum):
    PROPOSED = "proposed"
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class NoteKind(StrEnum):
    GENERAL = "general"
    RESEARCH = "research"
    INTERVIEW = "interview"
    OUTCOME = "outcome"


class PreferenceKind(StrEnum):
    REGION = "region"
    COMPANY = "company"
    TECHNOLOGY = "technology"
    ROLE = "role"
    EXCLUSION = "exclusion"


class RecipientKind(StrEnum):
    TO = "to"
    CC = "cc"
    BCC = "bcc"


class RequirementKind(StrEnum):
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    LOCATION = "location"
    WORK_AUTHORIZATION = "work_authorization"
    OTHER = "other"


class SourceKind(StrEnum):
    APIFY = "apify"
    LINKEDIN = "linkedin"
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    CAREER_PAGE = "career_page"
    COMPANY_WEBSITE = "company_website"
    GITHUB = "github"
    CONFERENCE = "conference"
    SEARCH = "search"
    MANUAL_FILE = "manual_file"
    OTHER = "other"


class SponsorMatchStatus(StrEnum):
    MATCHED = "matched"
    PROBABLE_ALIAS = "probable_alias"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class AssistantStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    ERROR = "error"


class ToolRisk(StrEnum):
    """How much a tool call can affect the world, which decides who must approve it."""

    READ = "read"
    WRITE = "write"
    SPEND = "spend"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    DONE = "done"
    FAILED = "failed"
