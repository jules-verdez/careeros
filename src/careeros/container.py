"""Application composition root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from careeros.agents.daily import DailyAgent
from careeros.agents.discovery import DiscoveryAgent
from careeros.agents.runs import RunLedger
from careeros.clock import Clock, SystemClock
from careeros.config import Settings
from careeros.connectors.apify import (
    ApifyGlassdoorConnector,
    ApifyGreenhouseConnector,
    ApifyLeverConnector,
    ApifyLinkedInProfileConnector,
)
from careeros.connectors.brave import BraveSearchConnector
from careeros.connectors.deliverability import DeliverabilityConnector
from careeros.connectors.github_commits import GitHubCommitEmailConnector
from careeros.connectors.gmail import GoogleWorkspaceMailConnector
from careeros.connectors.prospeo import ProspeoConnector
from careeros.connectors.registry import ConnectorRegistry
from careeros.connectors.sponsors import UkSponsorRegisterConnector
from careeros.connectors.team_pages import CompanyPageContactConnector
from careeros.connectors.vacancy import VacancyPageConnector
from careeros.database.enums import ActionKind
from careeros.database.session import Database
from careeros.prompts import PromptLibrary
from careeros.resources import resource_root
from careeros.services.applications import ApplicationService
from careeros.services.approvals import ApprovalService
from careeros.services.assessment import AssessmentService
from careeros.services.audit import AuditService
from careeros.services.contact_enrichment import ContactEnrichmentService
from careeros.services.contact_policy import ContactPolicy
from careeros.services.daily import DailyBriefService
from careeros.services.deliverability import DeliverabilityService
from careeros.services.email_drafts import EmailDraftExecutor
from careeros.services.email_sends import EmailSendExecutor
from careeros.services.inbox import InboxReconciliationService
from careeros.services.interviews import InterviewService
from careeros.services.job_ingestion import JobIngestionService
from careeros.services.opportunities import OpportunityService
from careeros.services.packets import PacketService
from careeros.services.people import PeopleService
from careeros.services.planning import NextActionPlanner
from careeros.services.profile import ProfileService
from careeros.services.research import ResearchService
from careeros.services.sponsorship import SponsorshipService
from careeros.services.tasks import TaskService
from careeros.services.vacancies import VacancyService


@dataclass
class Container:
    settings: Settings
    database: Database
    clock: Clock
    connectors: ConnectorRegistry
    prompts: PromptLibrary
    audit: AuditService
    approvals: ApprovalService
    contact_policy: ContactPolicy
    email_drafts: EmailDraftExecutor
    email_sends: EmailSendExecutor
    inbox: InboxReconciliationService
    contact_enrichment: ContactEnrichmentService
    deliverability: DeliverabilityService
    profiles: ProfileService
    assessments: AssessmentService
    opportunities: OpportunityService
    people: PeopleService
    research: ResearchService
    sponsorships: SponsorshipService
    ingestion: JobIngestionService
    planner: NextActionPlanner
    briefs: DailyBriefService
    ledger: RunLedger
    discovery_agent: DiscoveryAgent
    daily_agent: DailyAgent
    applications: ApplicationService
    packets: PacketService
    vacancies: VacancyService
    interviews: InterviewService
    tasks: TaskService

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        prompt_root: Path | None = None,
    ) -> Container:
        actual_clock = clock or SystemClock()
        database = Database(settings.database_url)
        audit = AuditService(actual_clock)
        approvals = ApprovalService(actual_clock, audit)
        profiles = ProfileService(audit)
        assessments = AssessmentService(actual_clock, audit)
        opportunities = OpportunityService(actual_clock)
        people = PeopleService(audit)
        research = ResearchService(audit)
        sponsorships = SponsorshipService(database, audit)
        ingestion = JobIngestionService(actual_clock, audit)
        applications = ApplicationService(actual_clock, audit)
        vacancies = VacancyService(actual_clock, audit, assessments)
        packets = PacketService(actual_clock, audit, applications, vacancies, approvals)
        approvals.validators[ActionKind.SUBMIT_APPLICATION] = packets.validate_action
        contact_policy = ContactPolicy(settings.do_not_contact)
        approvals.validators[ActionKind.CREATE_EMAIL_DRAFT] = contact_policy.validate_action
        approvals.validators[ActionKind.SEND_EMAIL] = contact_policy.validate_action
        interviews = InterviewService(applications, packets)
        planner = NextActionPlanner(actual_clock, audit, applications)
        briefs = DailyBriefService(
            actual_clock,
            audit,
            planner,
            opportunities,
            timezone=settings.timezone,
        )
        if prompt_root is None:
            prompt_root = resource_root() / "prompts"
        prompts = PromptLibrary(prompt_root)
        connectors = ConnectorRegistry()
        connectors.register("jobs.verification", VacancyPageConnector(actual_clock), validate=False)

        # Register external connectors lazily. Building a command container must not
        # make network or keyring calls; ``require`` validates immediately before use.
        for capability, connector_cls in [
            ("people.linkedin", ApifyLinkedInProfileConnector),
            ("jobs.greenhouse", ApifyGreenhouseConnector),
            ("jobs.lever", ApifyLeverConnector),
            ("jobs.glassdoor", ApifyGlassdoorConnector),
        ]:
            connectors.register(capability, connector_cls(), validate=False)

        # Avoid touching the OS keyring while constructing unrelated commands. The
        # registry validates this connector when a mail operation actually requires it.
        connectors.register("mail", GoogleWorkspaceMailConnector(), validate=False)
        connectors.register(
            "sponsors.ukvi",
            UkSponsorRegisterConnector(),
            validate=False,
        )
        connectors.register(
            "contact_enrichment.prospeo",
            ProspeoConnector(),
            validate=False,
        )
        connectors.register("search.brave", BraveSearchConnector(), validate=False)

        # Free boundaries. They cost nothing per call, so they exist to be run before any
        # metered connector: DNS decides whether an address can receive mail at all, and
        # the two discovery sources produce addresses their owners published themselves.
        connectors.register(
            "mail.deliverability",
            DeliverabilityConnector(clock=actual_clock),
            validate=False,
        )
        connectors.register(
            "people.github_commits",
            GitHubCommitEmailConnector(),
            validate=False,
        )
        connectors.register(
            "people.company_pages",
            CompanyPageContactConnector(),
            validate=False,
        )

        email_drafts = EmailDraftExecutor(
            database,
            connectors,
            actual_clock,
            audit,
            contact_policy=contact_policy,
        )
        email_sends = EmailSendExecutor(
            database,
            connectors,
            actual_clock,
            audit,
            approvals,
            contact_policy=contact_policy,
        )
        inbox = InboxReconciliationService(database, connectors, actual_clock, audit)
        contact_enrichment = ContactEnrichmentService(
            database,
            connectors,
            actual_clock,
            audit,
        )
        deliverability = DeliverabilityService(database, connectors, actual_clock, audit)

        ledger = RunLedger(database, actual_clock, audit)
        discovery_agent = DiscoveryAgent(database, ledger, prompts, ingestion)
        daily_agent = DailyAgent(database, ledger, prompts, briefs)
        return cls(
            settings=settings,
            database=database,
            clock=actual_clock,
            connectors=connectors,
            prompts=prompts,
            audit=audit,
            approvals=approvals,
            contact_policy=contact_policy,
            email_drafts=email_drafts,
            email_sends=email_sends,
            inbox=inbox,
            contact_enrichment=contact_enrichment,
            deliverability=deliverability,
            profiles=profiles,
            assessments=assessments,
            opportunities=opportunities,
            people=people,
            research=research,
            sponsorships=sponsorships,
            ingestion=ingestion,
            planner=planner,
            briefs=briefs,
            ledger=ledger,
            discovery_agent=discovery_agent,
            daily_agent=daily_agent,
            applications=applications,
            packets=packets,
            vacancies=vacancies,
            interviews=interviews,
            tasks=TaskService(audit),
        )
