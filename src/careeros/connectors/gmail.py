"""Gmail connector built on the Google Workspace CLI.

This connector creates drafts, reads the mailbox, and sends only an existing draft whose
provider-visible content still matches a separately approved packet. Draft creation and
sending are deliberately distinct capabilities so approving the former cannot trigger
the latter.

It drives the ``gws`` binary rather than a Google client library because that is the
credential store the readiness probe already validates: one OAuth session, encrypted in
the OS keyring, checked by ``career doctor``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from careeros.connectors.base import ConnectorNotConfiguredError, MailConnector
from careeros.database.enums import DeliveryEventKind
from careeros.domain.records import BounceReport, MailboxMessage
from careeros.process import CommandRunner, SubprocessCommandRunner

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MINIMAL_LOGIN_COMMAND = (
    "gws auth login --scopes "
    "https://www.googleapis.com/auth/gmail.compose,"
    "https://www.googleapis.com/auth/gmail.readonly,"
    "https://www.googleapis.com/auth/calendar.readonly"
)

#: Drafts are addressed by the idempotency key through this synthetic Message-ID, which
#: Gmail indexes and exposes to search as ``rfc822msgid:``. Gmail has no idempotency
#: header of its own, so re-running an approved action would otherwise create a second
#: identical draft.
MESSAGE_ID_DOMAIN = "careeros.local"

#: Sender domains used by applicant-tracking systems for candidate notifications.
ATS_NOTICE_SENDERS = (
    "ashbyhq.com",
    "greenhouse-mail.io",
    "greenhouse.io",
    "myworkdayjobs.com",
    "lever.co",
    "smartrecruiters.com",
    "teamtailor.com",
    "workable.com",
    "recruitee.com",
    "hibob.com",
    "personio.de",
    "pinpointhq.com",
    "icims.com",
    "successfactors.com",
)
ATS_NOTICE_SUBJECTS = (
    "your application",
    "application to",
    "thank you for your interest",
    "update on your application",
    "your candidacy",
)
IDEMPOTENCY_HEADER = "X-CareerOS-ID"


class DraftContentMismatchError(RuntimeError):
    """Raised when the Gmail draft differs from the separately approved packet."""


class GoogleWorkspaceMailConnector(MailConnector):
    """Reads mail and materializes separately approved Gmail actions."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        user_id: str = "me",
        timeout: float = 30.0,
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._user_id = user_id
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "google-workspace-gmail"

    def healthcheck(self) -> None:
        """Confirm gws exists, holds a valid session, and carries the Gmail scope."""
        if shutil.which("gws") is None:
            raise ConnectorNotConfiguredError(
                "The gws executable is not installed; Gmail drafting is unavailable."
            )
        payload = self._json(("gws", "auth", "status"))
        if not payload.get("token_valid"):
            raise ConnectorNotConfiguredError(
                f"gws has no valid Google session. Run: {MINIMAL_LOGIN_COMMAND}"
            )
        scopes = set(payload.get("scopes") or ())
        has_minimal_scopes = {GMAIL_COMPOSE_SCOPE, GMAIL_READONLY_SCOPE}.issubset(scopes)
        if GMAIL_SCOPE not in scopes and not has_minimal_scopes:
            raise ConnectorNotConfiguredError(
                "The Google session needs Gmail compose plus read-only access. "
                f"Run: {MINIMAL_LOGIN_COMMAND}"
            )

    def list_messages(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """List message identifiers received after an instant, without reading bodies.

        Bounce reconciliation only needs to know which messages exist; pulling bodies
        here would read mail the pipeline has no reason to see.
        """
        query = f"after:{after.strftime('%Y/%m/%d')} -in:sent -in:drafts"
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": self._user_id, "q": query}),
            )
        )
        return list(payload.get("messages") or ())

    def find_bounces(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """List delivery-failure notifications, which is the only inbox read this needs."""
        query = f"after:{after.strftime('%Y/%m/%d')} from:mailer-daemon"
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": self._user_id, "q": query}),
            )
        )
        return list(payload.get("messages") or ())

    def find_application_notices(self, *, after: datetime) -> Iterable[dict[str, Any]]:
        """List ATS notification messages by id; bodies are read only when reconciling."""
        senders = " OR ".join(f"from:{domain}" for domain in ATS_NOTICE_SENDERS)
        subjects = " OR ".join(f'subject:"{phrase}"' for phrase in ATS_NOTICE_SUBJECTS)
        query = (
            f"after:{after.strftime('%Y/%m/%d')} -in:sent -in:drafts "
            f"-from:mailer-daemon (({senders}) OR ({subjects}))"
        )
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": self._user_id, "q": query}),
            )
        )
        return list(payload.get("messages") or ())

    def get_message(self, external_message_id: str) -> MailboxMessage:
        """Read and structure one message already selected by the reconciliation service."""
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "messages",
                "get",
                "--params",
                json.dumps(
                    {
                        "userId": self._user_id,
                        "id": external_message_id,
                        "format": "raw",
                    }
                ),
            )
        )
        raw = payload.get("raw")
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Gmail message {external_message_id} returned no raw content.")
        message = self._parse_raw_message(raw)
        sender = self._first_address(message.get("From", ""))
        if not sender:
            raise RuntimeError(f"Gmail message {external_message_id} has no sender address.")
        received_at = self._received_at(message, payload)
        recipients = tuple(
            address
            for _, address in getaddresses(
                [message.get("To", ""), message.get("Cc", ""), message.get("Delivered-To", "")]
            )
            if address
        )
        references = tuple(message.get("References", "").split())
        return MailboxMessage(
            external_message_id=str(payload.get("id") or external_message_id),
            external_thread_id=(
                str(payload["threadId"]) if payload.get("threadId") is not None else None
            ),
            rfc822_message_id=message.get("Message-ID", "").strip() or None,
            in_reply_to=message.get("In-Reply-To", "").strip() or None,
            references=references,
            sender_address=sender,
            recipient_addresses=recipients,
            subject=message.get("Subject", "").strip() or "(no subject)",
            body=self._message_body(message),
            received_at=received_at,
            automated=self._is_automated(message),
            bounce=self._bounce_report(message, sender=sender),
        )

    def create_draft(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        in_reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Create a Gmail draft, or return the existing one for this idempotency key.

        A reply carries the parent's RFC 822 Message-ID in In-Reply-To and References and
        names the Gmail thread, so the recipient's client shows it in the conversation
        instead of as a new cold message.
        """
        self._assert_sender_identity(sender)
        existing = self._draft_for(idempotency_key)
        if existing is not None:
            return existing

        raw = base64.urlsafe_b64encode(
            self._message(
                sender=sender,
                recipient=recipient,
                subject=subject,
                body=body,
                idempotency_key=idempotency_key,
                in_reply_to=in_reply_to,
            ).as_bytes()
        ).decode("ascii")
        message_payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            message_payload["threadId"] = thread_id

        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "drafts",
                "create",
                "--params",
                json.dumps({"userId": self._user_id}),
                "--json",
                json.dumps({"message": message_payload}),
            )
        )
        draft_id = payload.get("id")
        if not draft_id:
            raise RuntimeError(f"Gmail accepted the request but returned no draft id: {payload}")
        return str(draft_id)

    def send_draft(
        self,
        external_draft_id: str,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        rfc822_message_id: str,
        external_thread_id: str,
    ) -> str:
        """Send one unchanged draft, recovering a prior send by its stable thread ID."""
        self._assert_sender_identity(sender)
        existing = self._sent_message_for(external_thread_id)
        if existing is not None:
            return existing

        payload = self._draft_payload(external_draft_id)
        raw = (payload.get("message") or {}).get("raw")
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Gmail draft {external_draft_id} returned no raw message.")
        self._assert_approved_content(
            raw,
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            idempotency_key=idempotency_key,
            rfc822_message_id=rfc822_message_id,
        )

        sent = self._json(
            (
                "gws",
                "gmail",
                "users",
                "drafts",
                "send",
                "--params",
                json.dumps({"userId": self._user_id}),
                "--json",
                json.dumps({"id": external_draft_id}),
            )
        )
        message_id = sent.get("id")
        if not message_id:
            raise RuntimeError(f"Gmail accepted the send but returned no message id: {sent}")
        return str(message_id)

    def draft_identity(self, external_draft_id: str) -> tuple[str, str]:
        """Read Gmail's pre-send Message-ID and stable thread identity."""
        payload = self._draft_payload(external_draft_id)
        draft_message = payload.get("message") or {}
        raw = draft_message.get("raw")
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Gmail draft {external_draft_id} returned no raw message.")
        message = self._parse_raw_message(raw)
        message_id = message.get("Message-ID", "").strip()
        if not message_id:
            raise RuntimeError(f"Gmail draft {external_draft_id} has no Message-ID header.")
        thread_id = draft_message.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError(f"Gmail draft {external_draft_id} has no thread ID.")
        return message_id, thread_id

    def _message(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        in_reply_to: str | None = None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = f"<{self.message_id(idempotency_key)}>"
        message[IDEMPOTENCY_HEADER] = self.idempotency_marker(idempotency_key)
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)
        return message

    @staticmethod
    def message_id(idempotency_key: str) -> str:
        # The stored key contains colons, which are not valid in the dot-atom portion of
        # an RFC 5322 Message-ID. A digest preserves stable retry identity without
        # leaking the database action key into the provider message.
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"careeros-{digest}@{MESSAGE_ID_DOMAIN}"

    @staticmethod
    def idempotency_marker(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    def _assert_sender_identity(self, sender: str) -> None:
        """Refuse to create a draft in a mailbox different from the approved sender."""
        profile = self._json(
            (
                "gws",
                "gmail",
                "users",
                "getProfile",
                "--params",
                json.dumps({"userId": self._user_id}),
            )
        )
        authenticated = str(profile.get("emailAddress") or "").strip().casefold()
        if not authenticated:
            raise RuntimeError("Gmail returned no authenticated mailbox identity.")
        if authenticated != sender.strip().casefold():
            raise RuntimeError(
                "The approved sender does not match the authenticated Gmail mailbox: "
                f"{sender} != {authenticated}."
            )

    def _draft_for(self, idempotency_key: str) -> str | None:
        """Find a draft by a custom header Gmail preserves when rewriting Message-ID."""
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "drafts",
                "list",
                "--params",
                json.dumps(
                    {
                        "userId": self._user_id,
                        "maxResults": 100,
                    }
                ),
            )
        )
        marker = self.idempotency_marker(idempotency_key)
        drafts = payload.get("drafts") or ()
        for draft in drafts:
            draft_id = draft.get("id")
            if not draft_id:
                continue
            raw = (self._draft_payload(str(draft_id)).get("message") or {}).get("raw")
            if not isinstance(raw, str) or not raw:
                continue
            if self._idempotency_header(self._parse_raw_message(raw)) == marker:
                return str(draft_id)
        return None

    def _sent_message_for(self, external_thread_id: str) -> str | None:
        """Recover a send that reached Gmail before the local transaction committed."""
        payload = self._json(
            (
                "gws",
                "gmail",
                "users",
                "threads",
                "get",
                "--params",
                json.dumps(
                    {
                        "userId": self._user_id,
                        "id": external_thread_id,
                        "format": "minimal",
                    }
                ),
            )
        )
        for message in payload.get("messages") or ():
            if message.get("id") and "SENT" in (message.get("labelIds") or ()):
                return str(message["id"])
        return None

    def _assert_approved_content(
        self,
        raw: str,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: str,
        rfc822_message_id: str,
    ) -> None:
        """Fail closed if Gmail's current draft is not the packet the user approved."""
        message = self._parse_raw_message(raw)

        actual_from = [address.casefold() for _, address in getaddresses([message.get("From", "")])]
        actual_to = [address.casefold() for _, address in getaddresses([message.get("To", "")])]
        body_part = message.get_body(preferencelist=("plain",))
        actual_body = body_part.get_content() if body_part is not None else ""
        mismatches = []
        if actual_from != [sender.casefold()]:
            mismatches.append("sender")
        if actual_to != [recipient.casefold()]:
            mismatches.append("recipient")
        if message.get("Subject", "") != subject:
            mismatches.append("subject")
        if self._normalise_body(actual_body) != self._normalise_body(body):
            mismatches.append("body")
        if message.get("Message-ID", "") != rfc822_message_id:
            mismatches.append("Message-ID")
        marker = self._idempotency_header(message)
        if marker is not None and marker != self.idempotency_marker(idempotency_key):
            mismatches.append(IDEMPOTENCY_HEADER)
        if mismatches:
            fields = ", ".join(mismatches)
            raise DraftContentMismatchError(
                f"Gmail draft changed after approval ({fields}); refusing to send."
            )

    @staticmethod
    def _idempotency_header(message: EmailMessage) -> str | None:
        """Ignore surrounding MIME folding whitespace, preserving the marker itself."""
        marker = message.get(IDEMPOTENCY_HEADER)
        return str(marker).strip(" \t\r\n") if marker is not None else None

    @staticmethod
    def _normalise_body(value: str) -> str:
        """Ignore MIME newline encoding and trailing transport newlines only."""
        return value.replace("\r\n", "\n").rstrip("\n")

    @staticmethod
    def _first_address(value: str) -> str:
        addresses = [address for _, address in getaddresses([value]) if address]
        return addresses[0] if addresses else ""

    @staticmethod
    def _received_at(message: EmailMessage, payload: dict[str, Any]) -> datetime:
        date_header = message.get("Date")
        if date_header:
            try:
                parsed = parsedate_to_datetime(date_header)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                pass
        internal_date = payload.get("internalDate")
        if isinstance(internal_date, str | int | float) and internal_date != "":
            try:
                return datetime.fromtimestamp(float(internal_date) / 1000.0, tz=UTC)
            except (TypeError, ValueError, OSError):
                pass
        raise RuntimeError("Gmail message has neither a usable Date nor internalDate.")

    @staticmethod
    def _message_body(message: EmailMessage) -> str:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            if message.get_content_maintype() == "text":
                content = message.get_content()
                return content if isinstance(content, str) else ""
            return ""
        content = part.get_content()
        return content if isinstance(content, str) else ""

    @staticmethod
    def _is_automated(message: EmailMessage) -> bool:
        auto_submitted = message.get("Auto-Submitted", "").strip().casefold()
        precedence = message.get("Precedence", "").strip().casefold()
        return (bool(auto_submitted) and auto_submitted != "no") or precedence in {
            "bulk",
            "junk",
            "list",
        }

    @classmethod
    def _bounce_report(cls, message: EmailMessage, *, sender: str) -> BounceReport | None:
        delivery_blocks: list[EmailMessage] = []
        original_message_id: str | None = None
        original_recipient: str | None = None
        for part in message.walk():
            if part.get_content_type() == "message/delivery-status":
                payload = part.get_payload()
                if isinstance(payload, list):
                    delivery_blocks.extend(
                        item for item in payload if isinstance(item, EmailMessage)
                    )
            elif part.get_content_type() == "message/rfc822":
                payload = part.get_payload()
                if isinstance(payload, list) and payload:
                    original = payload[0]
                    if isinstance(original, EmailMessage):
                        original_message_id = original.get("Message-ID", "").strip() or None
                        original_recipient = cls._first_address(original.get("To", "")) or None

        sender_local = sender.partition("@")[0].casefold()
        looks_like_daemon = sender_local in {"mailer-daemon", "postmaster"}
        if not delivery_blocks and not looks_like_daemon:
            return None

        fields: dict[str, str] = {}
        for block in delivery_blocks:
            for key in (
                "Action",
                "Status",
                "Final-Recipient",
                "Original-Recipient",
                "Diagnostic-Code",
                "Original-Message-ID",
            ):
                value = block.get(key)
                if value and key not in fields:
                    fields[key] = value.strip()

        action = fields.get("Action", "").casefold()
        status = fields.get("Status")
        if action == "failed" or (status and status.startswith("5")):
            kind = DeliveryEventKind.HARD_BOUNCE
        elif action == "delayed" or (status and status.startswith("4")):
            kind = DeliveryEventKind.DELAYED
        else:
            kind = DeliveryEventKind.SOFT_BOUNCE

        recipient_field = fields.get("Final-Recipient") or fields.get("Original-Recipient")
        recipient = cls._dsn_address(recipient_field) or original_recipient
        original_message_id = fields.get("Original-Message-ID") or original_message_id
        diagnostic = fields.get("Diagnostic-Code")
        if diagnostic is None:
            body = cls._message_body(message)
            match = re.search(r"(?im)^(?:diagnostic-code|reason):\s*(.+)$", body)
            diagnostic = match.group(1).strip() if match else None
        return BounceReport(
            kind=kind,
            recipient_address=recipient,
            smtp_status=status,
            diagnostic=diagnostic,
            original_message_id=original_message_id,
        )

    @staticmethod
    def _dsn_address(value: str | None) -> str | None:
        if not value:
            return None
        candidate = value.rsplit(";", maxsplit=1)[-1].strip().strip("<>")
        return candidate or None

    def _draft_payload(self, external_draft_id: str) -> dict[str, Any]:
        return self._json(
            (
                "gws",
                "gmail",
                "users",
                "drafts",
                "get",
                "--params",
                json.dumps(
                    {
                        "userId": self._user_id,
                        "id": external_draft_id,
                        "format": "raw",
                    }
                ),
            )
        )

    @staticmethod
    def _parse_raw_message(raw: str) -> EmailMessage:
        padded = raw + "=" * (-len(raw) % 4)
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(padded)
            )
        except Exception as error:
            raise DraftContentMismatchError(
                "Gmail draft is not a readable MIME message."
            ) from error
        if not isinstance(message, EmailMessage):
            raise DraftContentMismatchError("Gmail draft is not an EmailMessage.")
        return message

    def _json(self, args: tuple[str, ...]) -> dict[str, Any]:
        """Run a gws command and parse its JSON, tolerating its keyring preamble."""
        result = self._runner.run(args, timeout=self._timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(f"gws {' '.join(args[1:4])} failed: {detail[-1] if detail else ''}")
        return self._parse(result.stdout)

    @staticmethod
    def _parse(stdout: str) -> dict[str, Any]:
        """Extract the JSON object, which gws prints after a keyring backend notice."""
        lines = stdout.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip().startswith(("{", "[")):
                decoded = json.loads("\n".join(lines[index:]))
                return decoded if isinstance(decoded, dict) else {"items": decoded}
        return {}
