"""Free address deliverability checks: DNS always, SMTP only when authorized.

A domain without a mail exchanger cannot receive mail. Checking that before a paid
verification avoids spending a credit on an address the domain cannot deliver to.

Two checks live here and they are deliberately not the same kind of operation.

The mail-exchanger lookup is free, anonymous and effectively unlimited. It resolves over
DNS-over-HTTPS so the connector needs neither a resolver library nor a system binary,
matching the ``urllib``-only style of the Hunter and ATS connectors.

The SMTP probe opens a connection to somebody else's mail server and asks whether one
mailbox exists. That is a request against a third party, it is throttled in practice by
greylisting and sender reputation, and most consumer and cloud networks block outbound
port 25 outright. It therefore takes an explicit per-call authorization, exactly as the
paid connectors do, and is off by default.

What a probe can and cannot prove is asymmetric, and the asymmetry is the whole point.
A ``550`` is decisive: the mailbox does not exist. A ``250`` is only decisive at a domain
that rejects unknown recipients; at a catch-all domain, and at most Google Workspace and
Microsoft 365 tenants, every recipient is accepted and the answer means nothing. This
connector reports that difference rather than flattening it into a score.
"""

from __future__ import annotations

import json
import secrets
import smtplib
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from careeros.clock import Clock, SystemClock
from careeros.connectors.base import ConnectorNotConfiguredError, MailDeliverabilityConnector
from careeros.database.enums import ContactDeliveryStatus
from careeros.domain.records import DeliverabilityVerdict

#: Cloudflare's public resolver speaks JSON over HTTPS without a key or an account.
#: Google's ``dns.google/resolve`` is the fallback: two independent resolvers means a
#: single provider outage cannot silently turn every domain into "no mail exchanger",
#: which would otherwise mark a whole contact list undeliverable.
RESOLVER_URLS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
DNS_JSON_ACCEPT = "application/dns-json"
USER_AGENT = "careeros-deliverability"

#: DNS record type 15 is MX. The JSON APIs return the numeric type, not the name.
MX_RECORD_TYPE = 15
#: NXDOMAIN. The domain itself does not exist, so neither does any address at it.
DNS_STATUS_NXDOMAIN = 3

#: SMTP reply codes that mean the recipient was accepted for delivery.
ACCEPTED_CODES = frozenset({250, 251})
#: Permanent rejections that name the recipient rather than the sender or the policy.
#: ``550`` is the ordinary "user unknown"; ``551``, ``553`` and ``554`` also appear.
REJECTED_CODES = frozenset({550, 551, 553, 554})


@dataclass(frozen=True)
class MailDomain:
    """What the public DNS says about one domain's ability to receive mail."""

    domain: str
    exists: bool
    exchangers: tuple[str, ...]

    @property
    def can_receive_mail(self) -> bool:
        """True when a mail exchanger is published for the domain.

        A domain with an address record but no MX is technically still deliverable under
        RFC 5321 implicit-MX rules, and this returns False for it anyway. Every firm in
        this pipeline runs hosted mail with explicit MX records, so the implicit case in
        practice means a parked or dead domain, which is exactly what should be flagged.
        """
        return self.exists and bool(self.exchangers)


class DeliverabilityConnector(MailDeliverabilityConnector):
    """Judges whether an address can receive mail, without spending a vendor credit."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        smtp_timeout: float = 12.0,
        smtp_port: int = 25,
        helo_hostname: str = "careeros.local",
        probe_sender: str = "",
        clock: Clock | None = None,
    ) -> None:
        self._timeout = timeout
        self._smtp_timeout = smtp_timeout
        self._smtp_port = smtp_port
        self._helo_hostname = helo_hostname
        self._probe_sender = probe_sender
        self._clock = clock or SystemClock()

    @property
    def name(self) -> str:
        return "dns-smtp-deliverability"

    def healthcheck(self) -> None:
        """Confirm a public resolver answers. No key, no account, no credit."""
        try:
            self._resolve("cloudflare.com", MX_RECORD_TYPE)
        except Exception as error:
            # Any resolver failure is a configuration problem, not a verdict.
            msg = f"No DNS-over-HTTPS resolver is reachable: {error}"
            raise ConnectorNotConfiguredError(msg) from error

    # ------------------------------------------------------------------ DNS, free

    def mail_domain(self, domain: str) -> MailDomain:
        """Resolve one domain's mail exchangers over DNS-over-HTTPS."""
        normalized = domain.strip().rstrip(".").casefold()
        payload = self._resolve(normalized, MX_RECORD_TYPE)
        if int(payload.get("Status", 0)) == DNS_STATUS_NXDOMAIN:
            return MailDomain(domain=normalized, exists=False, exchangers=())
        hosts: list[str] = []
        for answer in payload.get("Answer") or ():
            if int(answer.get("type", 0)) != MX_RECORD_TYPE:
                continue
            # The record data is "<preference> <host>"; the host is what matters here.
            parts = str(answer.get("data", "")).split()
            if len(parts) == 2 and parts[1] not in (".", ""):
                hosts.append(parts[1].rstrip("."))
        return MailDomain(domain=normalized, exists=True, exchangers=tuple(hosts))

    def mail_exchangers(self, domain: str) -> tuple[str, ...]:
        """Return the domain's mail exchangers; empty when it cannot receive mail."""
        return self.mail_domain(domain).exchangers

    # ------------------------------------------------- SMTP, explicitly authorized

    def is_catch_all(self, domain: str, *, allow_smtp_probe: bool) -> bool | None:
        """Whether the domain accepts every recipient, so no address there verifies.

        Returns ``None`` when the question was not asked or could not be answered, which
        is a different fact from "no" and must not be collapsed into one.
        """
        if not allow_smtp_probe:
            return None
        exchangers = self.mail_exchangers(domain)
        if not exchangers:
            return None
        # A random local part cannot belong to anyone. A server that accepts it accepts
        # everything, so a 250 for the real address would prove nothing either.
        nonce = secrets.token_hex(12)
        code, _ = self._rcpt(exchangers[0], f"{nonce}@{domain}")
        if code in ACCEPTED_CODES:
            return True
        if code in REJECTED_CODES:
            return False
        return None

    def check(self, email: str, *, allow_smtp_probe: bool = False) -> DeliverabilityVerdict:
        """Judge one address, spending nothing and probing only when authorized."""
        address = email.strip()
        _, _, domain = address.partition("@")
        checked_at = self._clock.now()
        if not domain:
            return DeliverabilityVerdict(
                email=address,
                domain="",
                status=ContactDeliveryStatus.INVALID,
                method="syntax",
                detail="The value is not an email address: it has no domain part.",
                has_mx=False,
                is_catch_all=None,
                checked_at=checked_at,
            )

        try:
            mail_domain = self.mail_domain(domain)
        except Exception as error:
            # A resolver outage says nothing about the address, so it is not a verdict.
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.UNKNOWN,
                method="dns_mx",
                detail=f"DNS lookup failed, so nothing is known about this address: {error}",
                has_mx=False,
                is_catch_all=None,
                checked_at=checked_at,
            )

        if not mail_domain.exists:
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.INVALID,
                method="dns_mx",
                detail="The domain does not exist in public DNS.",
                has_mx=False,
                is_catch_all=None,
                checked_at=checked_at,
            )
        if not mail_domain.can_receive_mail:
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.INVALID,
                method="dns_mx",
                detail="The domain publishes no mail exchanger, so it cannot receive mail.",
                has_mx=False,
                is_catch_all=None,
                checked_at=checked_at,
            )

        if not allow_smtp_probe:
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.UNKNOWN,
                method="dns_mx",
                detail=(
                    f"The domain accepts mail through {mail_domain.exchangers[0]}. "
                    "The mailbox itself was not probed; re-run with an SMTP authorization "
                    "or spend one verification credit to decide it."
                ),
                has_mx=True,
                is_catch_all=None,
                checked_at=checked_at,
            )

        catch_all = self.is_catch_all(domain, allow_smtp_probe=True)
        code, message = self._rcpt(mail_domain.exchangers[0], address)
        detail = f"{mail_domain.exchangers[0]} answered {code}: {message}".strip()
        if code in REJECTED_CODES:
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.INVALID,
                method="smtp_probe",
                detail=f"The mail server rejected the recipient. {detail}",
                has_mx=True,
                is_catch_all=catch_all,
                checked_at=checked_at,
            )
        if code in ACCEPTED_CODES:
            if catch_all:
                return DeliverabilityVerdict(
                    email=address,
                    domain=domain,
                    status=ContactDeliveryStatus.RISKY,
                    method="smtp_probe",
                    detail=(
                        "The domain accepts every recipient, so acceptance of this address "
                        f"proves nothing about the mailbox. {detail}"
                    ),
                    has_mx=True,
                    is_catch_all=True,
                    checked_at=checked_at,
                )
            return DeliverabilityVerdict(
                email=address,
                domain=domain,
                status=ContactDeliveryStatus.DELIVERABLE,
                method="smtp_probe",
                detail=f"The mail server accepted the recipient. {detail}",
                has_mx=True,
                is_catch_all=catch_all,
                checked_at=checked_at,
            )
        return DeliverabilityVerdict(
            email=address,
            domain=domain,
            status=ContactDeliveryStatus.UNKNOWN,
            method="smtp_probe",
            detail=(
                "The mail server neither accepted nor rejected the recipient, which is "
                f"usually greylisting or a blocked probe rather than a bad address. {detail}"
            ),
            has_mx=True,
            is_catch_all=catch_all,
            checked_at=checked_at,
        )

    # ------------------------------------------------------------------- internals

    def _resolve(self, name: str, record_type: int) -> dict[str, Any]:
        """Ask each public resolver in turn until one answers."""
        query = urlencode({"name": name, "type": str(record_type)})
        last_error: Exception | None = None
        for root in RESOLVER_URLS:
            request = Request(
                f"{root}?{query}",
                headers={"accept": DNS_JSON_ACCEPT, "User-Agent": USER_AGENT},
            )
            try:
                with urlopen(request, timeout=self._timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                continue
            if isinstance(payload, dict):
                return payload
            last_error = RuntimeError(f"{root} returned a non-object DNS answer")
        raise RuntimeError(f"every DNS-over-HTTPS resolver failed: {last_error}")

    def _rcpt(self, exchanger: str, address: str) -> tuple[int, str]:
        """Ask one mail server whether it would accept a recipient, then hang up.

        The session stops before ``DATA``, so no message is ever transmitted. The null
        return path is used first because that is what a real bounce would carry; a
        server that refuses it is retried with a configured sender rather than recorded
        as a rejection of the recipient, which it is not.
        """
        try:
            with smtplib.SMTP(
                exchanger,
                self._smtp_port,
                timeout=self._smtp_timeout,
                local_hostname=self._helo_hostname,
            ) as smtp:
                smtp.ehlo_or_helo_if_needed()
                code, _ = smtp.mail("")
                if code >= 400 and self._probe_sender:
                    smtp.rset()
                    code, _ = smtp.mail(self._probe_sender)
                if code >= 400:
                    return 0, "the server refused the sender, so the recipient was never asked"
                rcpt_code, rcpt_message = smtp.rcpt(address)
                return int(rcpt_code), rcpt_message.decode("utf-8", "replace").strip()
        except (TimeoutError, smtplib.SMTPException, OSError) as error:
            return 0, f"the probe could not complete: {error}"


def probe_url(email: str) -> str:
    """A human-checkable citation for a DNS verdict, used in audit payloads."""
    _, _, domain = email.partition("@")
    return f"https://dns.google/resolve?name={quote(domain)}&type=MX"
