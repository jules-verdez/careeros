"""Operator do-not-contact exclusions, enforced at every outreach boundary.

The rule is checked when a draft is staged, when a send is queued, when either is
approved, and again immediately before execution. A human approval records consent to
an exact payload; it is not permission to contact someone the operator excluded.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.database.models import ActionRequest, Email, EmailRecipient, Person, PersonContact
from careeros.services.normalization import normalize_domain, normalize_email, normalize_text


class ContactExcludedError(RuntimeError):
    """An outreach step targets someone on the do-not-contact list."""


@dataclass(frozen=True)
class ExclusionMatch:
    entry: str
    rule: str

    def message(self, subject: str) -> str:
        return (
            f"{subject} is on the do-not-contact list ({self.rule} rule: {self.entry}). "
            "Approval cannot override an exclusion."
        )


class ContactPolicy:
    """Match people and addresses against exact, domain, and name exclusions."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.entries = tuple(entry.strip() for entry in entries if entry.strip())
        self._addresses: dict[str, str] = {}
        self._domains: dict[str, str] = {}
        self._names: dict[str, str] = {}
        for entry in self.entries:
            if entry.startswith("@"):
                self._domains[normalize_domain(entry[1:])] = entry
            elif "@" in entry:
                self._addresses[normalize_email(entry)] = entry
            else:
                self._names[normalize_text(entry)] = entry

    def match(
        self,
        *,
        addresses: Iterable[str] = (),
        names: Iterable[str] = (),
    ) -> ExclusionMatch | None:
        for address in addresses:
            normalized = normalize_email(address)
            if normalized in self._addresses:
                return ExclusionMatch(self._addresses[normalized], "address")
            domain = normalized.rpartition("@")[2]
            for blocked, entry in self._domains.items():
                if domain == blocked or domain.endswith(f".{blocked}"):
                    return ExclusionMatch(entry, "domain")
        for name in names:
            normalized = normalize_text(name)
            if normalized in self._names:
                return ExclusionMatch(self._names[normalized], "name")
        return None

    def person_match(self, session: Session, person_id: str) -> ExclusionMatch | None:
        person = session.get(Person, person_id)
        if person is None:
            return None
        return self.match(
            addresses=self._person_addresses(session, {person_id}),
            names=(person.full_name,),
        )

    def email_match(self, session: Session, email_id: str) -> ExclusionMatch | None:
        """Check every recipient and every person the message is linked to."""
        email = session.get(Email, email_id)
        if email is None:
            return None
        recipients = session.scalars(
            select(EmailRecipient).where(EmailRecipient.email_id == email_id)
        ).all()
        person_ids = {email.person_id} | {recipient.person_id for recipient in recipients}
        linked = {identity for identity in person_ids if identity}
        names = session.scalars(select(Person.full_name).where(Person.id.in_(linked))).all()
        return self.match(
            addresses=[
                *(recipient.address for recipient in recipients),
                *self._person_addresses(session, linked),
            ],
            names=names,
        )

    def action_match(self, session: Session, action: ActionRequest) -> ExclusionMatch | None:
        payload = action.payload_json
        recipient = payload.get("recipient")
        direct = self.match(addresses=(recipient,)) if isinstance(recipient, str) else None
        email_id = payload.get("email_id")
        if direct is not None or not isinstance(email_id, str):
            return direct
        return self.email_match(session, email_id)

    def check_person(self, session: Session, person_id: str) -> None:
        match = self.person_match(session, person_id)
        if match is not None:
            raise ContactExcludedError(match.message("This person"))

    def check_email(self, session: Session, email_id: str) -> None:
        match = self.email_match(session, email_id)
        if match is not None:
            raise ContactExcludedError(match.message(f"A recipient of email {email_id}"))

    def validate_action(self, session: Session, action: ActionRequest) -> None:
        """Approval validator for email actions."""
        match = self.action_match(session, action)
        if match is not None:
            raise ContactExcludedError(match.message(f"The recipient of action {action.id}"))

    @staticmethod
    def _person_addresses(session: Session, person_ids: set[str]) -> list[str]:
        if not person_ids:
            return []
        return list(
            session.scalars(
                select(PersonContact.normalized_value).where(
                    PersonContact.person_id.in_(person_ids),
                    PersonContact.contact_type == "email",
                )
            ).all()
        )
