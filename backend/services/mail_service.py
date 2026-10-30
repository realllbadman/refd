"""Outbound email over SMTP.

``send`` never raises: a missing configuration is logged and skipped, and a
delivery failure is logged on the server only, so SMTP problems never reach the
person using the site. Request forms queue their emails as background tasks,
which run after the response has been sent.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from fastapi import BackgroundTasks

from backend.config import Settings

logger = logging.getLogger("ledger")

SMTP_TIMEOUT_SECONDS = 20
SUBJECT_MAX = 150
# One plain ASCII address: no commas, angle brackets or whitespace, so a submitted
# value can never add a second recipient or a header.
_ADDRESS_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


class MailNotConfigured(RuntimeError):
    pass


def is_valid_address(value: str | None) -> bool:
    return bool(value) and len(value) <= 254 and _ADDRESS_RE.match(value) is not None


def sender(settings: Settings) -> tuple[str, str]:
    """(display name, address) for the From header; MAIL_FROM falls back to the support address."""
    name, address = parseaddr(settings.mail_from or "")
    return name, address or settings.support_email


def _build(settings: Settings, *, to: str, subject: str, body: str, reply_to: str | None, automatic: bool) -> EmailMessage:
    name, address = sender(settings)
    message = EmailMessage()
    message["From"] = formataddr((name, address))
    message["To"] = to
    message["Subject"] = " ".join(subject.split())[:SUBJECT_MAX]
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2])
    if reply_to:
        message["Reply-To"] = reply_to
    if automatic:
        message["Auto-Submitted"] = "auto-replied"  # RFC 3834: other auto-responders must not answer it
    message.set_content(body)
    return message


def deliver(settings: Settings, *, to: str, subject: str, body: str, reply_to: str | None = None,
            automatic: bool = False) -> None:
    """Send one message, raising on any failure. Use ``send`` unless you need the error."""
    if not settings.mail_configured:
        raise MailNotConfigured("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must all be set.")
    message = _build(settings, to=to, subject=subject, body=body, reply_to=reply_to, automatic=automatic)
    context = ssl.create_default_context()
    if settings.smtp_secure:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
    with server:
        if not settings.smtp_secure:
            server.starttls(context=context)  # refuses to continue without TLS
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message, from_addr=sender(settings)[1], to_addrs=[to])


def send(settings: Settings, *, kind: str, to: str, subject: str, body: str, reply_to: str | None = None,
         automatic: bool = False) -> bool:
    """Best-effort send. Returns whether the message was accepted; never raises."""
    if not settings.mail_configured:
        logger.warning("Email skipped (%s): SMTP is not configured.", kind)
        return False
    try:
        deliver(settings, to=to, subject=subject, body=body, reply_to=reply_to, automatic=automatic)
    except Exception as exc:  # noqa: BLE001 - email must never break the request that triggered it
        logger.error("Email failed (%s): %s: %s", kind, type(exc).__name__, exc)
        return False
    logger.info("Email sent (%s).", kind)
    return True


def _format_fields(fields: list[tuple[str, str]]) -> str:
    lines = []
    for label, value in fields:
        text = str(value)
        if "\n" in text or len(text) > 70:
            lines.append(f"\n{label}:\n{text}\n")
        else:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def queue_request_emails(
    tasks: BackgroundTasks,
    settings: Settings,
    *,
    kind: str,
    subject: str,
    fields: list[tuple[str, str]],
    submitter_email: str | None,
    ack_subject: str,
    ack_line: str,
) -> None:
    """Queue the team's notification and, when an address was given, the submitter's confirmation.

    The confirmation deliberately repeats nothing the submitter typed: anyone can enter
    someone else's address, so it must not be usable to send them arbitrary text.
    """
    note = "" if submitter_email else "\n\nNo email address was given, so replying to this email won't reach them."
    tasks.add_task(
        send, settings, kind=f"{kind} notification", to=settings.support_email,
        subject=f"[{kind}] {subject}",
        body=f"New website submission: {kind}.\n\n{_format_fields(fields)}{note}\n",
        reply_to=submitter_email,
    )
    if submitter_email:
        brand = sender(settings)[0] or settings.site_name
        tasks.add_task(
            send, settings, kind=f"{kind} confirmation", to=submitter_email, subject=ack_subject,
            body=(f"Hello,\n\n{ack_line}\n\n"
                  f"To add anything, reply to this email or write to {settings.support_email}.\n\n"
                  f"If you didn't contact us, you can ignore this email.\n\n{brand}\n"),
            automatic=True,
        )
