from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email import policy

import pytest

from careeros.connectors.base import ConnectorNotConfiguredError
from careeros.connectors.gmail import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPE,
    DraftContentMismatchError,
    GoogleWorkspaceMailConnector,
)
from careeros.process import CommandResult

VALID_SESSION = {
    "token_valid": True,
    "has_refresh_token": True,
    "encryption_valid": True,
    "scopes": [GMAIL_SCOPE],
}


@pytest.fixture(autouse=True)
def installed_gws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("careeros.connectors.gmail.shutil.which", lambda _command: "/fake/gws")


class FakeRunner:
    """Matches gws commands by their leading words, ignoring volatile JSON arguments."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...], *, timeout: float = 15.0) -> CommandResult:
        del timeout
        self.calls.append(args)
        for prefix, result in self.responses.items():
            if args[: len(prefix)] == prefix:
                return result
        raise AssertionError(f"unexpected command: {args}")


def _ok(payload: object) -> CommandResult:
    # gws prints a keyring notice before its JSON; the parser must survive it.
    return CommandResult(0, "Using keyring backend: keyring\n" + json.dumps(payload), "")


def test_healthcheck_names_the_command_that_fixes_it() -> None:
    runner = FakeRunner({("gws", "auth", "status"): _ok({"token_valid": False})})
    connector = GoogleWorkspaceMailConnector(runner)
    with pytest.raises(ConnectorNotConfiguredError, match="gws auth login"):
        connector.healthcheck()


def test_healthcheck_fails_closed_when_gws_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("careeros.connectors.gmail.shutil.which", lambda _command: None)
    runner = FakeRunner({})
    with pytest.raises(ConnectorNotConfiguredError, match="not installed"):
        GoogleWorkspaceMailConnector(runner).healthcheck()
    assert runner.calls == []


def test_healthcheck_rejects_a_session_without_the_gmail_scope() -> None:
    """A valid Google session is not automatically a session allowed to touch Gmail."""
    session = {**VALID_SESSION, "scopes": ["https://www.googleapis.com/auth/calendar"]}
    runner = FakeRunner({("gws", "auth", "status"): _ok(session)})
    with pytest.raises(ConnectorNotConfiguredError, match=r"gmail\.compose"):
        GoogleWorkspaceMailConnector(runner).healthcheck()


def test_healthcheck_passes_on_a_valid_scoped_session() -> None:
    runner = FakeRunner({("gws", "auth", "status"): _ok(VALID_SESSION)})
    GoogleWorkspaceMailConnector(runner).healthcheck()


def test_healthcheck_accepts_the_least_privilege_draft_and_read_scopes() -> None:
    session = {**VALID_SESSION, "scopes": [GMAIL_COMPOSE_SCOPE, GMAIL_READONLY_SCOPE]}
    runner = FakeRunner({("gws", "auth", "status"): _ok(session)})
    GoogleWorkspaceMailConnector(runner).healthcheck()


def test_create_draft_builds_a_message_and_returns_the_id() -> None:
    runner = FakeRunner(
        {
            ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
            ("gws", "gmail", "users", "drafts", "list"): _ok({"drafts": []}),
            ("gws", "gmail", "users", "drafts", "create"): _ok({"id": "draft-99"}),
        }
    )
    connector = GoogleWorkspaceMailConnector(runner)
    draft_id = connector.create_draft(
        sender="candidate@example.com",
        recipient="recipient.two@example.com",
        subject="Applied to the Python quant dev role",
        body="Hi Second,\n\nShort note.\n",
        idempotency_key="abc123",
    )
    assert draft_id == "draft-99"

    create = next(call for call in runner.calls if call[4] == "create")
    raw = json.loads(create[-1])["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "From: candidate@example.com" in decoded
    assert "To: recipient.two@example.com" in decoded
    assert "Applied to the Python quant dev role" in decoded
    # The key travels as the Message-ID so a retry can find this draft again.
    assert connector.message_id("abc123") in decoded


@pytest.mark.parametrize("fold_marker", [False, True])
def test_create_draft_is_idempotent(fold_marker: bool) -> None:
    """Re-running an approved action must not leave two identical drafts in the mailbox."""
    responses = {
        ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
        ("gws", "gmail", "users", "drafts", "list"): _ok({"drafts": [{"id": "draft-7"}]}),
        ("gws", "gmail", "users", "drafts", "get"): _ok({}),
    }
    runner = FakeRunner(responses)
    connector = GoogleWorkspaceMailConnector(runner)
    responses[("gws", "gmail", "users", "drafts", "get")] = _ok(
        {
            "id": "draft-7",
            "message": {
                "raw": _raw_message(
                    connector,
                    sender="candidate@example.com",
                    recipient="a@b.test",
                    subject="s",
                    body="b",
                    idempotency_key="dup",
                    fold_marker=fold_marker,
                )
            },
        }
    )
    assert (
        connector.create_draft(
            sender="candidate@example.com",
            recipient="a@b.test",
            subject="s",
            body="b",
            idempotency_key="dup",
        )
        == "draft-7"
    )
    # No create call was made at all.
    assert all(call[4] != "create" for call in runner.calls)


def test_create_draft_rejects_a_different_authenticated_mailbox() -> None:
    runner = FakeRunner(
        {("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "other@example.com"})}
    )
    connector = GoogleWorkspaceMailConnector(runner)
    with pytest.raises(RuntimeError, match="does not match"):
        connector.create_draft(
            sender="candidate@example.com",
            recipient="a@b.test",
            subject="s",
            body="b",
            idempotency_key="wrong-account",
        )


def _raw_message(
    connector: GoogleWorkspaceMailConnector,
    *,
    sender: str = "candidate@example.com",
    recipient: str = "recipient@example.com",
    subject: str = "Quantitative Analyst",
    body: str = "Hi Sample,\n\nThis is a demonstration message.\n",
    idempotency_key: str = "create-action-key",
    fold_marker: bool = False,
    marker_override: str | None = None,
) -> str:
    message = connector._message(
        sender=sender,
        recipient=recipient,
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
    )
    if marker_override is not None:
        message.replace_header("X-CareerOS-ID", marker_override)
    if fold_marker:
        raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
        marker = marker_override or connector.idempotency_marker(idempotency_key)
        header = f"X-CareerOS-ID: {marker}\r\n".encode()
        assert header in raw
        raw = raw.replace(header, f"X-CareerOS-ID:\r\n\t{marker}\t\r\n".encode(), 1)
    else:
        raw = message.as_bytes()
    return base64.urlsafe_b64encode(raw).decode("ascii")


@pytest.mark.parametrize("fold_marker", [False, True])
def test_send_draft_checks_the_exact_content_and_returns_message_id(fold_marker: bool) -> None:
    responses = {
        ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
        ("gws", "gmail", "users", "threads", "get"): _ok({"messages": []}),
        ("gws", "gmail", "users", "drafts", "get"): _ok({}),
        ("gws", "gmail", "users", "drafts", "send"): _ok({"id": "message-7"}),
    }
    runner = FakeRunner(responses)
    connector = GoogleWorkspaceMailConnector(runner)
    responses[("gws", "gmail", "users", "drafts", "get")] = _ok(
        {
            "id": "draft-1",
            "message": {
                "raw": _raw_message(connector, fold_marker=fold_marker),
                "threadId": "thread-1",
            },
        }
    )

    message_id = connector.send_draft(
        "draft-1",
        sender="candidate@example.com",
        recipient="recipient@example.com",
        subject="Quantitative Analyst",
        body="Hi Sample,\n\nThis is a demonstration message.\n",
        idempotency_key="create-action-key",
        rfc822_message_id=f"<{connector.message_id('create-action-key')}>",
        external_thread_id="thread-1",
    )

    assert message_id == "message-7"
    send = next(call for call in runner.calls if call[4] == "send")
    assert json.loads(send[-1]) == {"id": "draft-1"}


def test_send_draft_refuses_a_changed_idempotency_marker() -> None:
    responses = {
        ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
        ("gws", "gmail", "users", "threads", "get"): _ok({"messages": []}),
        ("gws", "gmail", "users", "drafts", "get"): _ok({}),
    }
    runner = FakeRunner(responses)
    connector = GoogleWorkspaceMailConnector(runner)
    responses[("gws", "gmail", "users", "drafts", "get")] = _ok(
        {
            "id": "draft-1",
            "message": {"raw": _raw_message(connector, marker_override="0" * 64)},
        }
    )
    with pytest.raises(DraftContentMismatchError, match="X-CareerOS-ID"):
        connector.send_draft(
            "draft-1",
            sender="candidate@example.com",
            recipient="recipient@example.com",
            subject="Quantitative Analyst",
            body="Hi Sample,\n\nThis is a demonstration message.\n",
            idempotency_key="create-action-key",
            rfc822_message_id=f"<{connector.message_id('create-action-key')}>",
            external_thread_id="thread-1",
        )
    assert all(call[4] != "send" for call in runner.calls)


def test_send_draft_refuses_a_provider_draft_edited_after_approval() -> None:
    responses = {
        ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
        ("gws", "gmail", "users", "threads", "get"): _ok({"messages": []}),
        ("gws", "gmail", "users", "drafts", "get"): _ok({}),
    }
    runner = FakeRunner(responses)
    connector = GoogleWorkspaceMailConnector(runner)
    responses[("gws", "gmail", "users", "drafts", "get")] = _ok(
        {
            "id": "draft-1",
            "message": {
                "raw": _raw_message(connector, body="Different body.\n"),
            },
        }
    )

    with pytest.raises(DraftContentMismatchError, match="body"):
        connector.send_draft(
            "draft-1",
            sender="candidate@example.com",
            recipient="recipient@example.com",
            subject="Quantitative Analyst",
            body="Hi Sample,\n\nThis is a demonstration message.\n",
            idempotency_key="create-action-key",
            rfc822_message_id=f"<{connector.message_id('create-action-key')}>",
            external_thread_id="thread-1",
        )
    assert all(call[4] != "send" for call in runner.calls)


def test_send_retry_recovers_the_sent_message_without_resending() -> None:
    runner = FakeRunner(
        {
            ("gws", "gmail", "users", "getProfile"): _ok({"emailAddress": "candidate@example.com"}),
            ("gws", "gmail", "users", "threads", "get"): _ok(
                {"messages": [{"id": "already-sent-1", "labelIds": ["SENT"]}]}
            ),
        }
    )
    connector = GoogleWorkspaceMailConnector(runner)

    message_id = connector.send_draft(
        "draft-that-no-longer-exists",
        sender="candidate@example.com",
        recipient="recipient@example.com",
        subject="Quantitative Analyst",
        body="Hi Sample,\n",
        idempotency_key="create-action-key",
        rfc822_message_id="<gmail-generated-id@gmail.test>",
        external_thread_id="stable-thread-1",
    )

    assert message_id == "already-sent-1"
    assert all(call[3:5] not in {("drafts", "get"), ("drafts", "send")} for call in runner.calls)


def test_bounce_search_targets_mailer_daemon_only() -> None:
    runner = FakeRunner(
        {("gws", "gmail", "users", "messages", "list"): _ok({"messages": [{"id": "m1"}]})}
    )
    connector = GoogleWorkspaceMailConnector(runner)
    found = list(connector.find_bounces(after=datetime(2026, 8, 12, tzinfo=UTC)))
    assert found == [{"id": "m1"}]

    query = json.loads(runner.calls[0][-1])["q"]
    assert "from:mailer-daemon" in query
    assert "after:2026/08/12" in query


def test_get_message_extracts_a_structured_hard_bounce() -> None:
    raw = """From: Mail Delivery Subsystem <mailer-daemon@googlemail.test>
To: candidate@example.com
Date: Thu, 20 Aug 2026 12:00:00 +0000
Message-ID: <dsn-1@googlemail.test>
Subject: Delivery Status Notification (Failure)
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn

--dsn
Content-Type: text/plain; charset=utf-8

The address could not be found.
--dsn
Content-Type: message/delivery-status

Final-Recipient: rfc822; bad@example.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 user unknown
Original-Message-ID: <original-1@example.com>

--dsn--
"""
    encoded = base64.urlsafe_b64encode(raw.replace("\n", "\r\n").encode()).decode()
    runner = FakeRunner(
        {
            ("gws", "gmail", "users", "messages", "get"): _ok(
                {
                    "id": "provider-dsn-1",
                    "threadId": "dsn-thread-1",
                    "raw": encoded,
                }
            )
        }
    )

    message = GoogleWorkspaceMailConnector(runner).get_message("provider-dsn-1")

    assert message.sender_address == "mailer-daemon@googlemail.test"
    assert message.bounce is not None
    assert message.bounce.kind.value == "hard_bounce"
    assert message.bounce.recipient_address == "bad@example.com"
    assert message.bounce.smtp_status == "5.1.1"
    assert message.bounce.original_message_id == "<original-1@example.com>"
    assert "user unknown" in (message.bounce.diagnostic or "")


def test_a_failing_gws_command_raises_with_its_message() -> None:
    runner = FakeRunner({("gws", "auth", "status"): CommandResult(1, "", "keyring is locked")})
    with pytest.raises(RuntimeError, match="keyring is locked"):
        GoogleWorkspaceMailConnector(runner).healthcheck()
