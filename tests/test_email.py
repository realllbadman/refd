"""Outgoing email: configuration, form routing, headers and failure handling."""

from __future__ import annotations

import smtplib

import pytest

from backend.config import Settings
from backend.services import mail_service
from tests.conftest import USER_PASSWORD, login, make_user, other_client, page_csrf

SUPPORT = "support@refundclaimers.com"
SMTP = dict(smtp_host="mail.example.test", smtp_user=SUPPORT, smtp_password="secret-smtp-pass",
            mail_from="RefundClaimers <support@refundclaimers.com>")
CLAIM = {"merchant": "Northwind", "order_reference": "NW-1", "purchase_date": "2026-09-01",
         "amount_cents": 4999, "reason": "damaged", "description": "Broken zipper."}


class FakeSMTP:
    sent: list = []
    logins: list = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        FakeSMTP.logins.append((user, password))

    def send_message(self, message, from_addr=None, to_addrs=None):
        FakeSMTP.sent.append((message, from_addr, to_addrs))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail_with = [], [], None
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _contact(client, **fields):
    token = page_csrf(client.get("/contact").text)
    form = {"name": "Sam Visitor", "email": "sam@example.com", "subject": "Question", "message": "How long does it take?",
            "csrf_token": token, **fields}
    return client.post("/contact", data=form, follow_redirects=False)


def _user(client):
    make_user(client, "alice")
    browser = other_client(client)
    browser.headers.update(login(browser, "alice", USER_PASSWORD))
    return browser


def test_contact_form_notifies_team_and_confirms(make_client, smtp):
    client = make_client(**SMTP)
    assert _contact(client).status_code == 303
    assert len(smtp.sent) == 2
    notification, sender, recipients = smtp.sent[0]
    assert recipients == [SUPPORT] and sender == SUPPORT
    assert notification["From"] == "RefundClaimers <support@refundclaimers.com>"
    assert notification["Reply-To"] == "sam@example.com"
    body = notification.get_content()
    for value in ("Sam Visitor", "sam@example.com", "Question", "How long does it take?"):
        assert value in body
    confirmation, _, recipients = smtp.sent[1]
    assert recipients == ["sam@example.com"]
    assert confirmation["Auto-Submitted"] == "auto-replied"
    assert smtp.logins == [(SUPPORT, "secret-smtp-pass")] * 2


def test_confirmation_repeats_nothing_the_submitter_typed(make_client, smtp):
    client = make_client(**SMTP)
    _contact(client, name="BUY-NOW-SPAM", subject="SPAM-SUBJECT", message="SPAM-BODY http://bad.example")
    confirmation = smtp.sent[1][0]
    text = confirmation.as_string()
    assert "SPAM" not in text and "bad.example" not in text


def test_contact_without_email_skips_confirmation_and_reply_to(make_client, smtp):
    client = make_client(**SMTP)
    assert _contact(client, email="").status_code == 303
    assert len(smtp.sent) == 1
    assert smtp.sent[0][0]["Reply-To"] is None
    assert "No email address was given" in smtp.sent[0][0].get_content()


def test_honeypot_and_invalid_email_send_nothing(make_client, smtp):
    client = make_client(**SMTP)
    assert _contact(client, website="http://spam.example").status_code == 303
    assert _contact(client, email="a@b.com,victim@c.com").status_code == 422
    assert _contact(client, email="sam@example.com\r\nBcc: victim@c.com").status_code == 422
    assert smtp.sent == []


def test_support_form_emails_and_keeps_address(make_client, smtp):
    client = make_client(**SMTP)
    browser = _user(client)
    response = browser.post("/api/user/messages/", json={"subject": "Hi", "body": "Need help", "email": "alice@example.com"})
    assert response.status_code == 201
    assert [m[2] for m in smtp.sent] == [[SUPPORT], ["alice@example.com"]]
    assert smtp.sent[0][0]["Reply-To"] == "alice@example.com"
    assert "@alice" in smtp.sent[0][0].get_content()
    assert browser.post("/api/user/messages/", json={"subject": "Hi", "body": "x", "email": "nope"}).status_code == 422


def test_claim_form_emails_all_fields(make_client, smtp):
    client = make_client(**SMTP)
    browser = _user(client)
    response = browser.post("/api/user/claims/", json={**CLAIM, "contact_email": "alice@example.com"})
    assert response.status_code == 201, response.text
    claim_id = response.json()["id"]
    notification = smtp.sent[0][0]
    assert notification["Reply-To"] == "alice@example.com"
    assert f"[Refund claim] Claim #{claim_id}" in notification["Subject"]
    for value in ("Northwind", "NW-1", "2026-09-01", "$49.99", "Item arrived damaged", "Broken zipper.", "@alice"):
        assert value in notification.get_content()
    assert f"claim #{claim_id}" in smtp.sent[1][0].get_content()
    assert "Northwind" not in smtp.sent[1][0].as_string()


def test_claim_rejects_bad_email_without_creating_it(make_client, smtp):
    client = make_client(**SMTP)
    browser = _user(client)
    bad = browser.post("/api/user/claims/", json={**CLAIM, "contact_email": "not-an-email"})
    assert bad.status_code == 422
    assert browser.get("/api/user/claims/").json() == []
    assert smtp.sent == []


def test_claim_edit_does_not_accept_contact_email(make_client, smtp):
    client = make_client(**SMTP)
    browser = _user(client)
    claim = browser.post("/api/user/claims/", json=CLAIM).json()
    assert browser.post(f"/api/user/claims/{claim['id']}/", json={**CLAIM, "contact_email": "a@b.co"}).status_code == 422


def test_unconfigured_mail_is_skipped_and_logged(make_client, monkeypatch, caplog):
    def explode(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(smtplib, "SMTP_SSL", explode)
    client = make_client()
    assert _contact(client).status_code == 303
    assert "SMTP is not configured" in caplog.text


def test_smtp_failure_is_logged_but_never_shown(make_client, smtp, caplog):
    smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    client = make_client(**SMTP)
    response = _contact(client)
    assert response.status_code == 303 and response.headers["location"] == "/contact?sent=1"
    assert "bad credentials" not in client.get("/contact?sent=1").text
    assert "Email failed" in caplog.text
    assert "secret-smtp-pass" not in caplog.text


def test_password_not_in_settings_repr():
    settings = Settings(secret_key="x" * 40, smtp_password="secret-smtp-pass")
    assert "secret-smtp-pass" not in repr(settings)


def test_settings_from_env(monkeypatch):
    for key, value in {"SMTP_HOST": "mail.privateemail.com", "SMTP_PORT": "465", "SMTP_SECURE": "true", "SMTP_USER": SUPPORT,
                       "SMTP_PASSWORD": "pw", "MAIL_FROM": SMTP["mail_from"]}.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings.mail_configured and settings.smtp_secure and settings.smtp_port == 465
    assert mail_service.sender(settings) == ("RefundClaimers", SUPPORT)


def test_address_shown_on_site_from_one_setting(make_client):
    client = make_client(support_email="help@example.test")
    for path in ("/", "/contact", "/about", "/faq"):
        assert 'mailto:help@example.test' in client.get(path).text, path
    assert SUPPORT not in client.get("/contact").text


@pytest.mark.parametrize("bad", ["", "plain", "a@b", "a b@c.com", "a@b.com,c@d.com", "<a@b.com>", "a@b.com\nX: y", "é@b.com"])
def test_invalid_addresses(bad):
    assert not mail_service.is_valid_address(bad)


def test_valid_address():
    assert mail_service.is_valid_address("first.last+tag@mail.example.co.uk")
