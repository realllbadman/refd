import csv
import io
import sqlite3

from sqlalchemy import select

from backend.models import AuditEntry, ContactMessage, User
from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    adjust_balance,
    api_login,
    login,
    make_user,
    other_client,
    page_csrf,
)


def _db(client):
    return client.app.state.session_factory()


def register(client, username="new.member", password="member-pass-1", confirm=None, accept=True, **extra):
    token = page_csrf(client.get("/register").text)
    data = {
        "username": username, "password": password, "confirm_password": confirm or password,
        "display_name": extra.pop("display_name", ""), "csrf_token": token, **extra,
    }
    if accept:
        data["accept_terms"] = "1"
    return client.post("/register", data=data, follow_redirects=False)


# --- public website ----------------------------------------------------------------


def test_public_pages_render_for_visitors(anon):
    for path in ("/", "/about", "/faq", "/contact", "/terms", "/register", "/login"):
        response = anon.get(path)
        assert response.status_code == 200, path
        assert "refundclaimers" in response.text.lower(), path
        assert "classroom prototype" not in response.text.lower(), path
        assert "demonstration records only" not in response.text.lower(), path
        if path != "/register":
            assert 'href="/register"' in response.text, path


def test_home_shows_dashboard_link_when_signed_in(app_client, alice):
    client = other_client(app_client)
    login(client, "alice", USER_PASSWORD)
    response = client.get("/")
    assert response.status_code == 200
    assert "Open my account" in response.text


# --- self-registration ----------------------------------------------------------------


def test_register_creates_user_account_and_signs_in(anon):
    response = register(anon, username="New.Member", display_name="New Member")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard?welcome=1"
    me = anon.get("/api/me/").json()
    assert me["username"] == "new.member"
    assert me["display_name"] == "New Member"
    assert me["role"] == "user" and me["status"] == "active"
    assert me["balance_cents"] == 0
    assert me["signup_source"] == "self"
    assert "Welcome to" in anon.get("/dashboard?welcome=1").text
    with _db(anon) as db:
        actions = {e.action for e in db.scalars(select(AuditEntry)).all()}
        assert "account.registered" in actions


def test_register_validation(anon):
    make_user(anon, "taken")
    cases = [
        (dict(username="ab"), 422),
        (dict(username="taken"), 409),
        (dict(username="admin"), 409),  # reserved
        (dict(password="short"), 422),
        (dict(confirm="different-pass-1"), 422),
        (dict(accept=False), 422),
    ]
    for kwargs, status in cases:
        response = register(anon, **{"username": "valid.name", **kwargs})
        assert response.status_code == status, kwargs
    assert anon.get("/api/me/").status_code == 401


def test_register_ignores_role_fields_and_requires_csrf(anon):
    response = register(anon, username="sneaky", role="admin", status="inactive", balance_cents="99999")
    assert response.status_code == 303
    me = anon.get("/api/me/").json()
    assert me["role"] == "user" and me["balance_cents"] == 0
    fresh = other_client(anon)
    response = fresh.post("/register", data={"username": "nocsrf", "password": "member-pass-1",
                                              "confirm_password": "member-pass-1", "accept_terms": "1"})
    assert response.status_code == 400


def test_registration_can_be_closed_by_admin(admin_client):
    response = admin_client.post("/api/admin/settings/", json={"registration_open": False, "public_notice": "Closed for maintenance"})
    assert response.status_code == 200 and response.json()["registration_open"] is False
    visitor = other_client(admin_client)
    page = visitor.get("/register").text
    assert "currently closed" in page
    assert "Closed for maintenance" in visitor.get("/").text
    assert register(visitor, username="late.comer").status_code == 403
    admin_client.post("/api/admin/settings/", json={"registration_open": True})
    assert register(visitor, username="late.comer").status_code == 303


def test_users_cannot_change_settings(user_client):
    assert user_client.post("/api/admin/settings/", json={"registration_open": False}).status_code == 403


# --- contact & support messages -------------------------------------------------------------


def test_contact_form_stores_message(admin_client):
    visitor = other_client(admin_client)
    token = page_csrf(visitor.get("/contact").text)
    response = visitor.post("/contact", data={
        "name": "Sam Visitor", "email": "sam@example.com", "subject": "Question", "message": "How does it work?",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/contact?sent=1"
    messages = admin_client.get("/api/admin/messages/").json()
    assert messages["total"] == 1
    assert messages["items"][0]["subject"] == "Question"
    assert admin_client.get("/api/admin/stats/").json()["unread_messages"] == 1


def test_contact_form_validation_and_honeypot(admin_client):
    visitor = other_client(admin_client)
    token = page_csrf(visitor.get("/contact").text)
    bad = visitor.post("/contact", data={"name": "", "subject": "", "message": "", "csrf_token": token})
    assert bad.status_code == 422
    bad_email = visitor.post("/contact", data={"name": "A", "email": "not-an-email", "subject": "S", "message": "M", "csrf_token": token})
    assert bad_email.status_code == 422
    spam = visitor.post("/contact", data={"name": "Bot", "subject": "Buy", "message": "spam", "website": "http://x", "csrf_token": token},
                        follow_redirects=False)
    assert spam.status_code == 303
    assert admin_client.get("/api/admin/messages/?status=").json()["total"] == 0


def test_contact_rate_limit(admin_client):
    visitor = other_client(admin_client)
    token = page_csrf(visitor.get("/contact").text)
    for i in range(5):
        assert visitor.post("/contact", data={"name": "A", "subject": f"S{i}", "message": "M", "csrf_token": token},
                            follow_redirects=False).status_code == 303
    assert visitor.post("/contact", data={"name": "A", "subject": "S", "message": "M", "csrf_token": token}).status_code == 429


def test_support_message_reply_flow(admin_client, user_client, bob):
    sent = user_client.post("/api/user/messages/", json={"subject": "Balance question", "body": "Why did it change?"})
    assert sent.status_code == 201
    message_id = sent.json()["id"]
    inbox = admin_client.get("/api/admin/messages/").json()["items"]
    assert inbox[0]["username"] == "alice"
    reply = admin_client.post(f"/api/admin/messages/{message_id}/", json={"reply": "It was a class bonus."})
    assert reply.status_code == 200 and reply.json()["status"] == "read"
    mine = user_client.get("/api/user/messages/").json()
    assert mine[0]["reply"] == "It was a class bonus."
    # Another account holder never sees alice's messages.
    bob_client = other_client(admin_client)
    bob_client.headers.update(login(bob_client, "bob", USER_PASSWORD))
    assert bob_client.get("/api/user/messages/").json() == []
    assert admin_client.post(f"/api/admin/messages/{message_id}/", json={"status": "archived"}).json()["status"] == "archived"
    assert user_client.post(f"/api/admin/messages/{message_id}/", json={"status": "new"}).status_code == 403


# --- refund claims -------------------------------------------------------------------------------


def test_refund_claim_lifecycle_and_internal_note_privacy(admin_client, user_client, bob):
    payload = {
        "merchant": "Northstar Outfitters",
        "order_reference": "ORD-80421",
        "purchase_date": "2026-10-12",
        "amount_cents": 8499,
        "reason": "damaged",
        "description": "The jacket arrived with a broken zipper.",
    }
    created = user_client.post("/api/user/claims/", json=payload)
    assert created.status_code == 201, created.text
    claim_id = created.json()["id"]
    assert created.json()["status"] == "submitted"
    assert created.json()["events"][0]["event_type"] == "submitted"
    assert user_client.get("/dashboard/claims").status_code == 200
    assert admin_client.get("/admin/claims").status_code == 200

    own_claims = user_client.get("/api/user/claims/").json()
    assert [claim["id"] for claim in own_claims] == [claim_id]
    assert admin_client.get("/api/admin/claims/").json()["total"] == 1

    bob_client = other_client(admin_client)
    bob_client.headers.update(login(bob_client, "bob", USER_PASSWORD))
    assert bob_client.get("/api/user/claims/").json() == []
    assert bob_client.post(f"/api/user/claims/{claim_id}/messages/", json={"message": "Can I see this?"}).status_code == 404
    assert bob_client.post(f"/api/user/claims/{claim_id}/cancel/", json={}).status_code == 404

    reviewed = admin_client.post(f"/api/admin/claims/{claim_id}/", json={
        "status": "under_review",
        "priority": "high",
        "assign_to_me": True,
        "public_reply": "We are reviewing the order details.",
        "internal_note": "Check the seller response before deciding.",
    })
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["assigned_admin_id"] is not None
    assert reviewed.json()["priority"] == "high"
    assert any(event["event_type"] == "admin_note" and event["is_internal"] for event in reviewed.json()["events"])

    visible = user_client.get("/api/user/claims/").json()[0]
    assert visible["status"] == "under_review"
    assert "We are reviewing" in " ".join(event["message"] or "" for event in visible["events"])
    assert "Check the seller response" not in " ".join(event["message"] or "" for event in visible["events"])

    follow_up = user_client.post(f"/api/user/claims/{claim_id}/messages/", json={"message": "I have attached the receipt."})
    assert follow_up.status_code == 200
    cancelled = user_client.post(f"/api/user/claims/{claim_id}/cancel/", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert user_client.post(f"/api/user/claims/{claim_id}/messages/", json={"message": "Another update"}).status_code == 422


def test_refund_claim_validation_and_role_boundaries(make_client, admin_client, user_client):
    anon = make_client()
    assert anon.get("/api/user/claims/").status_code == 401
    assert admin_client.get("/api/user/claims/").status_code == 403
    assert user_client.get("/api/admin/claims/").status_code == 403
    bad = user_client.post("/api/user/claims/", json={
        "merchant": "Store",
        "order_reference": "ORDER-1",
        "purchase_date": "2026-10-12",
        "amount_cents": 0,
        "reason": "other",
        "description": "An issue with my order.",
    })
    assert bad.status_code == 422


# --- announcements --------------------------------------------------------------------------------


def test_announcements(admin_client, user_client):
    created = admin_client.post("/api/admin/announcements/", json={"title": "Welcome", "body": "Hello class", "level": "success"})
    assert created.status_code == 201
    draft = admin_client.post("/api/admin/announcements/", json={"title": "Draft", "body": "Hidden", "published": False})
    titles = [a["title"] for a in user_client.get("/api/user/announcements/").json()]
    assert titles == ["Welcome"]
    admin_client.post(f"/api/admin/announcements/{draft.json()['id']}/", json={"title": "Draft", "body": "Now live", "published": True, "pinned": True})
    titles = [a["title"] for a in user_client.get("/api/user/announcements/").json()]
    assert titles[0] == "Draft"  # pinned first
    assert admin_client.delete(f"/api/admin/announcements/{created.json()['id']}/").status_code == 200
    assert [a["title"] for a in user_client.get("/api/user/announcements/").json()] == ["Draft"]
    assert user_client.post("/api/admin/announcements/", json={"title": "x", "body": "y"}).status_code == 403
    assert admin_client.post("/api/admin/announcements/", json={"title": "", "body": "y"}).status_code == 422


# --- notes, password reset, profile ------------------------------------------------------------------


def test_admin_notes(admin_client, user_client, alice):
    note = admin_client.post(f"/api/admin/users/{alice.id}/notes/", json={"body": "Met in class"})
    assert note.status_code == 201
    notes = admin_client.get(f"/api/admin/users/{alice.id}/notes/").json()
    assert notes[0]["body"] == "Met in class" and notes[0]["author_username"] == ADMIN_USERNAME
    assert user_client.get(f"/api/admin/users/{alice.id}/notes/").status_code == 403
    assert "Met in class" not in user_client.get("/dashboard").text
    assert admin_client.delete(f"/api/admin/users/{alice.id}/notes/{note.json()['id']}/").status_code == 200
    assert admin_client.get(f"/api/admin/users/{alice.id}/notes/").json() == []


def test_admin_password_reset(admin_client, user_client, alice):
    response = admin_client.post(f"/api/admin/users/{alice.id}/password-reset/")
    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    assert user_client.get("/api/me/").status_code == 401  # existing session revoked
    fresh = other_client(admin_client)
    assert api_login(fresh, "alice", USER_PASSWORD).status_code == 401
    assert api_login(fresh, "alice", temporary).status_code == 200


def test_user_change_password_keeps_current_session(app_client, alice):
    first = other_client(app_client)
    first.headers.update(login(first, "alice", USER_PASSWORD))
    second = other_client(app_client)
    login(second, "alice", USER_PASSWORD)

    wrong = first.post("/api/user/password/", json={"current_password": "nope-nope", "new_password": "brand-new-pass-1"})
    assert wrong.status_code == 422
    ok = first.post("/api/user/password/", json={"current_password": USER_PASSWORD, "new_password": "brand-new-pass-1"})
    assert ok.status_code == 200
    first.headers["X-CSRF-Token"] = ok.json()["csrf_token"]
    assert first.get("/api/me/").status_code == 200
    assert second.get("/api/me/").status_code == 401
    assert api_login(other_client(app_client), "alice", "brand-new-pass-1").status_code == 200


def test_profile_update(user_client):
    response = user_client.post("/api/user/profile/", json={"display_name": "  Alice   Smith "})
    assert response.status_code == 200 and response.json()["display_name"] == "Alice Smith"
    assert user_client.post("/api/user/profile/", json={"display_name": "x" * 61}).status_code == 422
    assert user_client.post("/api/user/profile/", json={"display_name": "Ok", "role": "admin"}).status_code == 422


# --- bulk actions, exports, ledger -----------------------------------------------------------------------


def _admin_id(client):
    with _db(client) as db:
        return db.scalar(select(User.id).where(User.username == ADMIN_USERNAME))


def test_bulk_adjust_reports_each_account(admin_client, alice, bob):
    adjust_balance(admin_client, bob.id, 100)
    response = admin_client.post("/api/admin/users/bulk/", json={
        "user_ids": [alice.id, bob.id, _admin_id(admin_client)], "action": "adjust", "amount_cents": -50, "reason": "Fee demo",
    })
    body = response.json()
    assert body["succeeded"] == 1 and body["failed"] == 2  # alice would go negative; admin holds no balance
    assert admin_client.get(f"/api/admin/users/{bob.id}/").json()["user"]["balance_cents"] == 50
    response = admin_client.post("/api/admin/users/bulk/", json={"user_ids": [alice.id, bob.id], "action": "adjust", "amount_cents": 1000})
    assert response.json()["succeeded"] == 2


def test_bulk_status(admin_client, alice, bob):
    response = admin_client.post("/api/admin/users/bulk/", json={"user_ids": [alice.id, bob.id, _admin_id(admin_client)], "action": "deactivate"})
    body = response.json()
    assert body["succeeded"] == 2 and body["failed"] == 1  # cannot deactivate yourself
    assert admin_client.get("/api/admin/users/?status=inactive").json()["total"] == 2


def test_bulk_requires_admin_and_valid_payload(admin_client, user_client, alice):
    assert user_client.post("/api/admin/users/bulk/", json={"user_ids": [alice.id], "action": "activate"}).status_code == 403
    assert admin_client.post("/api/admin/users/bulk/", json={"user_ids": [], "action": "activate"}).status_code == 422
    assert admin_client.post("/api/admin/users/bulk/", json={"user_ids": [alice.id], "action": "adjust"}).status_code == 422


def test_account_filters_and_sort(admin_client, alice, bob):
    adjust_balance(admin_client, alice.id, 500)
    adjust_balance(admin_client, bob.id, 900)
    ranked = admin_client.get("/api/admin/users/?role=user&sort=balance_desc").json()["items"]
    assert [u["username"] for u in ranked] == ["bob", "alice"]
    assert admin_client.get("/api/admin/users/?role=admin").json()["total"] == 1
    assert admin_client.get("/api/admin/users/?sort=bogus").status_code == 422


def test_csv_exports_are_safe(admin_client):
    created = admin_client.post("/api/admin/users/", json={"username": "csv.user", "password": "csv-pass-123", "display_name": "=HYPERLINK(1)"}).json()
    adjust_balance(admin_client, created["user"]["id"], -0 + 1234, reason="=cmd|' /C calc'!A0")
    users = admin_client.get("/api/admin/users/export.csv")
    assert users.status_code == 200 and users.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(users.text)))
    assert rows[0][:3] == ["id", "username", "display_name"]
    row = next(r for r in rows if r[1] == "csv.user")
    assert row[2] == "'=HYPERLINK(1)" and row[5] == "12.34"
    activity = admin_client.get(f"/api/admin/activity/export.csv?user_id={created['user']['id']}")
    rows = list(csv.reader(io.StringIO(activity.text)))
    assert rows[1][8].startswith("'=") and rows[1][6] == "12.34"


def test_exports_require_admin(user_client):
    assert user_client.get("/api/admin/users/export.csv").status_code == 403
    assert user_client.get("/api/admin/activity/export.csv").status_code == 403


def test_global_ledger_filters(admin_client, alice, bob):
    adjust_balance(admin_client, alice.id, 500, reason="Alice credit")
    adjust_balance(admin_client, bob.id, 700)
    adjust_balance(admin_client, bob.id, -200)
    everything = admin_client.get("/api/admin/activity/").json()
    assert everything["total"] == 3 and everything["items"][0]["username"] == "bob"
    assert admin_client.get("/api/admin/activity/?q=alice").json()["total"] == 1
    assert admin_client.get("/api/admin/activity/?direction=debit").json()["total"] == 1
    assert admin_client.get("/api/admin/activity/?from=2000-01-01&to=2000-01-02").json()["total"] == 0


def test_user_activity_filters(admin_client, user_client, alice):
    adjust_balance(admin_client, alice.id, 500)
    adjust_balance(admin_client, alice.id, -100)
    assert user_client.get("/api/user/activity/?direction=credit").json()["total"] == 1
    assert user_client.get("/api/user/activity/?type=set").json()["total"] == 0
    summary = user_client.get("/api/user/summary/").json()
    assert summary["total_credits_cents"] == 500 and summary["total_debits_cents"] == -100
    assert [p["balance_cents"] for p in summary["history"]] == [500, 400]


def test_overview_and_charts(admin_client, alice):
    adjust_balance(admin_client, alice.id, 2500)
    overview = admin_client.get("/api/admin/overview/").json()
    assert overview["recent_activity"][0]["username"] == "alice"
    assert overview["top_balances"][0]["username"] == "alice"
    charts = admin_client.get("/api/admin/charts/?days=7").json()
    assert len(charts["series"]) == 7
    today = charts["series"][-1]
    assert today["signups"] == 1 and today["net_cents"] == 2500 and today["changes"] == 1
    stats = admin_client.get("/api/admin/stats/").json()
    assert stats["new_users_7d"] == 1 and stats["average_balance_cents"] == 2500


def test_audit_log(admin_client, alice):
    adjust_balance(admin_client, alice.id, 300, reason="Audit me")
    admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "inactive"})
    entries = admin_client.get("/api/admin/audit/").json()["items"]
    actions = [e["action"] for e in entries]
    assert "balance.adjust" in actions and "account.deactivated" in actions and "auth.login" in actions
    balance_entry = next(e for e in entries if e["action"] == "balance.adjust")
    assert "Audit me" in balance_entry["details"] and balance_entry["target_username"] == "alice"
    assert admin_client.get("/api/admin/audit/?action=balance").json()["total"] == 1


def test_audit_log_is_append_only(admin_client):
    import sqlalchemy

    with _db(admin_client) as db:
        for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
            try:
                db.execute(sqlalchemy.text(statement))
                db.commit()
                raise AssertionError("trigger did not fire")
            except sqlalchemy.exc.DatabaseError as exc:
                db.rollback()
                assert "append-only" in str(exc)


# --- pages -----------------------------------------------------------------------------------------------


def test_all_app_pages_render(admin_client, user_client, alice):
    for path in ("/admin", "/admin/accounts", "/admin/activity", "/admin/messages", "/admin/announcements",
                 "/admin/audit", "/admin/settings", f"/admin/users/{alice.id}"):
        assert admin_client.get(path).status_code == 200, path
    for path in ("/dashboard", "/dashboard/activity", "/dashboard/support", "/dashboard/profile"):
        assert user_client.get(path).status_code == 200, path
        assert user_client.get(path.replace("/dashboard", "/admin")).status_code in (403, 404)


# --- schema upgrade --------------------------------------------------------------------------------------


def test_old_database_gets_new_columns(make_settings, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(32) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(10) NOT NULL, status VARCHAR(10) NOT NULL, balance_cents INTEGER NOT NULL,
            session_version INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
    """)
    conn.close()
    settings = make_settings(database_url=f"sqlite:///{db_path}")
    with TestClient(create_app(settings)) as client:
        assert api_login(client, ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200
    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")}
    assert {"display_name", "last_login_at", "signup_source"} <= columns
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM contact_messages").fetchone()[0] == 0
    assert ContactMessage.__tablename__ == "contact_messages"


# --- live chat widget -----------------------------------------------------------


def _has_chat(response) -> bool:
    return "js/smartsupp.js" in response.text and "smartsupp" in response.headers["Content-Security-Policy"]


def test_chat_loads_on_signed_out_public_pages(anon):
    for path in ("/", "/about", "/faq", "/contact", "/terms", "/no-such-page"):
        assert _has_chat(anon.get(path)), path


def test_chat_never_loads_on_password_pages(anon):
    for path in ("/login", "/register"):
        response = anon.get(path)
        assert "js/smartsupp.js" not in response.text, path
        assert "smartsupp" not in response.headers["Content-Security-Policy"], path


def test_chat_loads_for_signed_in_account_holders(user_client):
    for path in ("/", "/dashboard", "/dashboard/activity", "/dashboard/claims", "/dashboard/support", "/no-such-page"):
        assert _has_chat(user_client.get(path)), path


def test_chat_never_loads_on_profile_or_for_admins(user_client, admin_client):
    for client, paths in ((user_client, ("/dashboard/profile",)), (admin_client, ("/", "/admin", "/admin/claims"))):
        for path in paths:
            response = client.get(path)
            assert "js/smartsupp.js" not in response.text, path
            assert "smartsupp" not in response.headers["Content-Security-Policy"], path
