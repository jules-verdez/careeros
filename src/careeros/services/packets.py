"""Versioned, human-reviewed evidence and deterministic application materials."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.clock import Clock
from careeros.database.enums import ActionKind, ActionStatus
from careeros.database.models import (
    ActionRequest,
    Application,
    ApplicationPacket,
    ApplicationWorkspace,
    CandidateProfile,
    CandidateVersion,
    Company,
    Job,
    JobRequirement,
)
from careeros.domain.job_review import EligibilityLabel
from careeros.domain.workflow import CandidateSnapshot, PacketSpec, VacancyEvidence, VacancyStatus
from careeros.services.applications import ApplicationService, require
from careeros.services.approvals import ApprovalService
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload
from careeros.services.vacancies import VacancyService


class PacketService:
    def __init__(
        self,
        clock: Clock,
        audit: AuditService,
        applications: ApplicationService,
        vacancies: VacancyService,
        approvals: ApprovalService,
    ) -> None:
        self.clock = clock
        self.audit = audit
        self.applications = applications
        self.vacancies = vacancies
        self.approvals = approvals

    @staticmethod
    def latest_version(session: Session, profile_id: str) -> CandidateVersion | None:
        return session.scalar(
            select(CandidateVersion)
            .where(CandidateVersion.candidate_profile_id == profile_id)
            .order_by(CandidateVersion.version.desc())
            .limit(1)
        )

    def import_profile(
        self, session: Session, profile_id: str, snapshot: CandidateSnapshot, *, reviewer: str
    ) -> CandidateVersion:
        require(session, CandidateProfile, profile_id)
        if not reviewer.strip():
            raise ValueError("A named human reviewer is required")
        payload = snapshot.model_dump(mode="json")
        digest = hash_payload(payload)
        latest = self.latest_version(session, profile_id)
        if latest and latest.sha256 == digest:
            return latest
        version = CandidateVersion(
            candidate_profile_id=profile_id,
            version=latest.version + 1 if latest else 1,
            payload_json=payload,
            sha256=digest,
            reviewed_by=reviewer.strip(),
            created_at=self.clock.now(),
        )
        session.add(version)
        session.flush()
        self.audit.append(
            session,
            actor=reviewer,
            event_type="profile.version_imported",
            aggregate_type="candidate_profile",
            aggregate_id=profile_id,
            payload={"version_id": version.id, "version": version.version, "sha256": digest},
        )
        return version

    def prepare(
        self, session: Session, application_id: str, spec: PacketSpec, *, actor: str
    ) -> ApplicationPacket:
        app = require(session, Application, application_id)
        workspace = require(session, ApplicationWorkspace, application_id)
        job = require(session, Job, app.job_id)
        company = require(session, Company, job.company_id)
        version = self.latest_version(session, workspace.candidate_profile_id)
        if version is None:
            raise ValueError(
                "Import reviewed candidate evidence with `career profile import` first"
            )
        if hash_payload(version.payload_json) != version.sha256:
            raise ValueError("Candidate version content changed; restore the reviewed version")
        profile = CandidateSnapshot.model_validate(version.payload_json)
        facts = {fact.key: fact for fact in profile.facts}
        selected = list(dict.fromkeys(spec.evidence_keys))
        requested = set(selected) | {key for answer in spec.answers for key in answer.evidence_keys}
        if requested - facts.keys():
            raise ValueError(f"Unknown candidate evidence keys: {sorted(requested - facts.keys())}")
        cv = [
            f"# {profile.name}",
            profile.headline,
            f"Target: {job.title} — {company.canonical_name}",
            "",
        ]
        for key in selected:
            cv.append(f"- {facts[key].text}")
        answers = [
            {
                "question": answer.question,
                "answer": "\n\n".join(
                    facts[key].text for key in dict.fromkeys(answer.evidence_keys)
                ),
                "evidence_keys": list(answer.evidence_keys),
            }
            for answer in spec.answers
        ]
        verification = self.vacancies.latest(session, job.id, workspace.candidate_profile_id)
        requirements = session.scalars(
            select(JobRequirement).where(JobRequirement.job_id == job.id)
        ).all()
        payload: dict[str, Any] = {
            "application_id": app.id,
            "candidate_version_id": version.id,
            "candidate_version_sha256": version.sha256,
            "candidate_snapshot": profile.model_dump(mode="json"),
            "job": {
                "id": job.id,
                "title": job.title,
                "company": company.canonical_name,
                "location": job.location,
                "source_url": verification.source_url if verification else None,
            },
            "verification_id": verification.id if verification else None,
            "verification": verification.evidence_json if verification else None,
            "specification": spec.model_dump(mode="json"),
            "cv_markdown": "\n\n".join(cv) + "\n",
            "answers": answers,
            "evidence": {key: facts[key].model_dump(mode="json") for key in sorted(requested)},
            "requirements_to_review": [requirement.text for requirement in requirements],
            "assembly": "Selected facts copied verbatim from reviewed candidate evidence; "
            "requirements are a checklist, not claims of satisfaction.",
        }
        digest = hash_payload(payload)
        existing = session.scalar(
            select(ApplicationPacket).where(
                ApplicationPacket.application_id == app.id, ApplicationPacket.sha256 == digest
            )
        )
        if existing:
            return existing
        packet = ApplicationPacket(
            application_id=app.id,
            candidate_version_id=version.id,
            verification_id=verification.id if verification else None,
            payload_json=payload,
            sha256=digest,
            created_at=self.clock.now(),
        )
        session.add(packet)
        session.flush()
        self.applications.event(
            session,
            app.id,
            "packet_prepared",
            {
                "packet_id": packet.id,
                "sha256": digest,
                "candidate_version_id": version.id,
            },
            actor,
        )
        return packet

    def validate_ready(self, session: Session, packet_id: str) -> ApplicationPacket:
        packet = require(session, ApplicationPacket, packet_id)
        app = require(session, Application, packet.application_id)
        workspace = require(session, ApplicationWorkspace, app.id)
        if app.applied_at is not None:
            raise ValueError("Application was already submitted")
        from careeros.database.enums import ApplicationStatus

        if app.status != ApplicationStatus.DRAFT:
            raise ValueError("Only a draft application can request submission approval")
        if hash_payload(packet.payload_json) != packet.sha256:
            raise ValueError("Packet content changed after preparation")
        latest_profile = self.latest_version(session, workspace.candidate_profile_id)
        if latest_profile is None or latest_profile.id != packet.candidate_version_id:
            raise ValueError("Candidate profile changed; prepare a new packet")
        if hash_payload(latest_profile.payload_json) != latest_profile.sha256:
            raise ValueError("Candidate version content changed")
        verification = self.vacancies.latest(session, app.job_id, workspace.candidate_profile_id)
        if verification is None or verification.id != packet.verification_id:
            raise ValueError("Prepare a packet with the latest vacancy verification")
        if not self.vacancies.current(verification):
            raise ValueError("Vacancy verification is stale; recheck within 48 hours of approval")
        evidence = VacancyEvidence.model_validate(verification.evidence_json)
        if hash_payload(verification.evidence_json) != verification.sha256:
            raise ValueError("Verification content changed")
        if evidence.status != VacancyStatus.OPEN or (
            evidence.deadline_at and evidence.deadline_at <= self.clock.now()
        ):
            raise ValueError("Vacancy is not verified open for submission")
        if (
            not evidence.review
            or evidence.review.eligibility_label != EligibilityLabel.CONFIRMED
            or evidence.review.unresolved_fact
            or not verification.reviewed_by
        ):
            raise ValueError(
                "A current human eligibility review must resolve material concerns first"
            )
        latest_assessment = next(
            (
                row
                for row in self.vacancies.assessments.latest(
                    session, profile_id=workspace.candidate_profile_id
                )
                if row.job.id == app.job_id
            ),
            None,
        )
        if latest_assessment:
            from careeros.services.opportunities import OpportunityService

            card = OpportunityService.describe(latest_assessment)
            if card.eligibility_label != EligibilityLabel.CONFIRMED or card.unresolved_fact:
                raise ValueError(
                    "The current assessment still has unresolved eligibility or material concerns"
                )
        return packet

    def validate_action(self, session: Session, action: ActionRequest) -> None:
        packet_id = action.payload_json.get("packet_id")
        if not isinstance(packet_id, str) or action.connector_name != "manual-application":
            raise ValueError("Submission approval requires a prepared application packet")
        packet = self.validate_ready(session, packet_id)
        expected = {
            "packet_id": packet.id,
            "packet_sha256": packet.sha256,
            "packet": packet.payload_json,
        }
        if hash_payload(expected) != action.payload_sha256:
            raise ValueError("Application approval no longer matches its frozen packet")

    def request_approval(self, session: Session, packet_id: str, *, actor: str) -> ActionRequest:
        packet = self.validate_ready(session, packet_id)
        key = f"application-packet:{packet.id}:{packet.sha256}"
        existing = session.scalar(select(ActionRequest).where(ActionRequest.idempotency_key == key))
        if existing:
            if existing.status in {ActionStatus.REJECTED, ActionStatus.CANCELLED}:
                raise ValueError(
                    "This packet was rejected or cancelled; "
                    "revise the materials before requesting again"
                )
            return existing
        # A complete frozen payload can be reviewed with the existing action commands.
        # There is deliberately no browser/form executor behind this connector.
        return self.approvals.request(
            session,
            kind=ActionKind.SUBMIT_APPLICATION,
            summary=f"Review application packet for {packet.payload_json['job']['title']}",
            connector_name="manual-application",
            payload={
                "packet_id": packet.id,
                "packet_sha256": packet.sha256,
                "packet": packet.payload_json,
            },
            idempotency_key=key,
            requested_by=actor,
        )

    @staticmethod
    def render(packet: ApplicationPacket) -> str:
        if hash_payload(packet.payload_json) != packet.sha256:
            raise ValueError("Packet content changed")
        payload = packet.payload_json
        lines = [
            "# Application review pack",
            f"Packet: {packet.id}",
            f"SHA-256: {packet.sha256}",
            "## CV",
            payload["cv_markdown"],
            "## Application answers",
        ]
        for answer in payload["answers"]:
            lines.extend(
                [
                    f"### {answer['question']}",
                    answer["answer"],
                    f"Evidence: {', '.join(answer['evidence_keys'])}",
                ]
            )
        lines.append("## Evidence sources")
        for key, fact in payload["evidence"].items():
            lines.append(f"- {key}: {fact['source']}")
        lines.append("## Requirements to review")
        lines.extend(f"- {text}" for text in payload["requirements_to_review"])
        lines.extend(
            [
                "## Vacancy evidence",
                str(payload["verification"] or "Not yet verified"),
                "Local preparation only. Approval and actual submission are separate steps.",
            ]
        )
        return "\n\n".join(lines) + "\n"
