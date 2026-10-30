"""Check outgoing email: python -m backend.send_test_email [recipient]

Sends one message straight away using the SMTP_* settings and prints the real
result, including the SMTP error if it fails. The password is never printed.
"""

from __future__ import annotations

import sys

from backend.config import Settings
from backend.services import mail_service


def main() -> int:
    settings = Settings.from_env()
    recipient = sys.argv[1] if len(sys.argv) > 1 else settings.support_email
    name, address = mail_service.sender(settings)
    print(f"Server:   {settings.smtp_host or '(SMTP_HOST not set)'}:{settings.smtp_port} "
          f"({'implicit TLS' if settings.smtp_secure else 'STARTTLS'})")
    print(f"Login:    {settings.smtp_user or '(SMTP_USER not set)'}")
    print(f"Password: {'set' if settings.smtp_password else '(SMTP_PASSWORD not set)'}")
    print(f"From:     {name} <{address}>")
    print(f"To:       {recipient}")
    if not mail_service.is_valid_address(recipient):
        print("FAILED: that recipient is not a valid email address.")
        return 2
    try:
        mail_service.deliver(
            settings, to=recipient, subject=f"Test email from {settings.site_name}",
            body=f"This is a test from {settings.site_name}. If you can read it, outgoing email works.\n",
        )
    except Exception as exc:  # noqa: BLE001 - this tool's whole job is to report the failure
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("OK: the mail server accepted the message. Check the inbox (and spam folder).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
