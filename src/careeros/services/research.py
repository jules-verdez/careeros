"""Immutable company research snapshots."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.enums import DocumentKind
from careeros.database.models import Company, CompanyDocument, Document, ResearchReport
from careeros.services.audit import AuditService
from careeros.services.normalization import hash_payload, normalize_text, sha256_text


class ResearchService:
    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    def import_snapshot(
        self,
        session: Session,
        *,
        company_name: str,
        as_of_date: date,
        markdown: str,
        structured: dict[str, Any],
        confidence: float,
        actor: str,
    ) -> ResearchReport:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        company = session.scalar(
            select(Company).where(Company.normalized_name == normalize_text(company_name))
        )
        if company is None:
            raise LookupError(f"company not found: {company_name}")
        existing = session.scalar(
            select(ResearchReport).where(
                ResearchReport.company_id == company.id,
                ResearchReport.as_of_date == as_of_date,
            )
        )
        if existing is not None:
            msg = (
                f"research snapshot already exists for {company.canonical_name} "
                f"on {as_of_date.isoformat()}"
            )
            raise ValueError(msg)
        markdown_document = self._text_document(
            session,
            title=f"{company.canonical_name} research — {as_of_date.isoformat()}",
            kind=DocumentKind.COMPANY_RESEARCH_MARKDOWN,
            content=markdown,
            media_type="text/markdown",
        )
        json_document = self._json_document(
            session,
            title=f"{company.canonical_name} research data — {as_of_date.isoformat()}",
            kind=DocumentKind.COMPANY_RESEARCH_JSON,
            content=structured,
        )
        report = ResearchReport(
            company_id=company.id,
            as_of_date=as_of_date,
            markdown_document_id=markdown_document.id,
            json_document_id=json_document.id,
            confidence=confidence,
        )
        session.add(report)
        session.flush()
        for document, relationship in (
            (markdown_document, "research_markdown"),
            (json_document, "research_json"),
        ):
            session.add(
                CompanyDocument(
                    company_id=company.id,
                    document_id=document.id,
                    relationship=relationship,
                )
            )
        self._audit.append(
            session,
            actor=actor,
            event_type="company.research_snapshot_imported",
            aggregate_type="company",
            aggregate_id=company.id,
            payload={
                "research_report_id": report.id,
                "as_of_date": as_of_date.isoformat(),
                "confidence": confidence,
                "markdown_sha256": markdown_document.sha256,
                "json_sha256": json_document.sha256,
            },
        )
        return report

    @staticmethod
    def _text_document(
        session: Session,
        *,
        title: str,
        kind: DocumentKind,
        content: str,
        media_type: str,
    ) -> Document:
        digest = sha256_text(content)
        document = session.scalar(select(Document).where(Document.sha256 == digest))
        if document is None:
            document = Document(
                kind=kind,
                title=title,
                media_type=media_type,
                content_text=content,
                sha256=digest,
            )
            session.add(document)
            session.flush()
        return document

    @staticmethod
    def _json_document(
        session: Session,
        *,
        title: str,
        kind: DocumentKind,
        content: dict[str, Any],
    ) -> Document:
        digest = hash_payload(content)
        document = session.scalar(select(Document).where(Document.sha256 == digest))
        if document is None:
            document = Document(
                kind=kind,
                title=title,
                media_type="application/json",
                content_json=content,
                sha256=digest,
            )
            session.add(document)
            session.flush()
        return document
