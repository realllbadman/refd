"""Approve-and-credit, approvals, claim queues, saved replies, read state, recovery codes,
account closing and administrator two-step sign-in."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import select, update

from backend.models import BalanceActivity, RefundClaim, User, utcnow
from backend.security import totp_code
from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    api_login,
    csrf_token,
    login,
    make_user,
    other_client,
    page_csrf,
)

CLAIM = {
    "merchant": "Northwind Outfitters",
    "order_reference": "NW-1001",
    "purchase_date": "2026-09-01",
    "amount_cents": 4999,
    "reason": "damaged",
    "description": "The jacket arrived with a broken zipper.",
}


def _claim(user_client, **overrides) -> dict:
    response = user_client.post("/api/user/claims/", json={**CLAIM, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def _second_admin(admin_client, username="second.admin", password="second-admin-pass"):
    make_user(admin_client, username, password, role="admin")
    client = other_client(admin_client)
    client.headers.update(login(client, username, password))
    return client


def _balance(app, user_id: int) -> int:
    with app.state.session_factory() as db:
        return db.get(User, user_id).balance_cents


# --- approve and credit ---------------------------------------------------------------


def test_approve_and_credit_links_claim_and_ledger(admin_client, user_client, alice):
    claim = _claim(user_client)
    response = admin_client.post(f"/api/admin/claims/{claim['id']}/credit/",
                                 json={"credit_cents": 4500, "public_reply": "Refund approved, minus shipping."})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["pending"] is None
    assert body["claim"]["status"] == "approved"
    assert body["claim"]["credit_cents"] == 4500
    assert _balance(admin_client.app, alice.id) == 4500

    activity = user_client.get("/api/user/activity/").json()["items"][0]
    assert activity["claim_id"] == claim["id"]
    assert activity["claim_merchant"] == CLAIM["merchant"]
    mine = user_client.get("/api/user/claims/").json()[0]
    assert mine["credit_cents"] == 4500
    assert "Refund approved, minus shipping." in [e["message"] for e in mine["events"]]

    again = admin_client.post(f"/api/admin/claims/{claim['id']}/credit/", json={"credit_cents": 100})
    assert again.status_code == 422  # closed now
    assert _balance(admin_client.app, alice.id) == 4500


def test_credit_validation(admin_client, user_client, alice):
    claim = _claim(user_client)
    url = f"/api/admin/claims/{claim['id']}/credit/"
    assert admin_client.post(url, json={"credit_cents": 5000}).status_code == 422  # above claimed amount
    assert admin_client.post(url, json={"credit_cents": 0}).status_code == 422
    assert user_client.post(url, json={"credit_cents": 100}).status_code == 403
    assert _balance(admin_client.app, alice.id) == 0
    assert admin_client.get(f"/api/admin/claims/{claim['id']}/").json()["status"] == "submitted"


def test_claim_can_only_be_credited_once_at_database_level(admin_client, user_client, alice):
    claim = _claim(user_client)
    admin_client.post(f"/api/admin/claims/{claim['id']}/credit/", json={"credit_cents": 100})
    db_path = admin_client.app.state.settings.database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO balance_activity (user_id, admin_id, change_type, previous_cents, amount_cents, resulting_cents, "
            "created_at, claim_id) VALUES (?, 1, 'adjust', 100, 1, 101, '2026-01-01', ?)", (alice.id, claim["id"]))
        raise AssertionError("duplicate claim credit was accepted")
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# --- second-administrator approval -------------------------------------------------------


def test_settings_threshold_requires_two_admins(admin_client):
    response = admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 10000})
    assert response.status_code == 409
    _second_admin(admin_client)
    response = admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 10000, "stale_claim_days": 7})
    assert response.status_code == 200
    assert response.json()["approval_threshold_cents"] == 10000
    assert response.json()["stale_claim_days"] == 7


def test_large_change_waits_for_a_different_admin(admin_client, alice):
    second = _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 10000})

    small = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "adjust", "amount_cents": 9999})
    assert small.status_code == 201
    big = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "adjust", "amount_cents": 25000})
    assert big.status_code == 202, big.text
    request_id = big.json()["pending"]["id"]
    assert _balance(admin_client.app, alice.id) == 9999

    assert admin_client.post(f"/api/admin/approvals/{request_id}/approve/").status_code == 403  # own request
    approved = second.post(f"/api/admin/approvals/{request_id}/approve/")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert _balance(admin_client.app, alice.id) == 34999
    assert second.post(f"/api/admin/approvals/{request_id}/approve/").status_code == 409  # already decided

    entry = admin_client.get(f"/api/admin/users/{alice.id}/activity/").json()["items"][0]
    assert entry["admin_username"] == ADMIN_USERNAME
    assert entry["approved_by_username"] == "second.admin"


def test_set_mode_threshold_uses_the_size_of_the_change(admin_client, alice):
    _second_admin(admin_client)
    admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "set", "target_cents": 50000})
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 10000})
    near = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "set", "target_cents": 55000})
    assert near.status_code == 201  # only +$50
    far = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "set", "target_cents": 1000})
    assert far.status_code == 202  # -$540


def test_reject_and_cancel_requests(admin_client, alice):
    second = _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 100})
    first = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "adjust", "amount_cents": 500}).json()
    rejected = second.post(f"/api/admin/approvals/{first['pending']['id']}/reject/", json={"note": "Wrong account"})
    assert rejected.json()["status"] == "rejected"
    other = admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "adjust", "amount_cents": 700}).json()
    cancelled = admin_client.post(f"/api/admin/approvals/{other['pending']['id']}/reject/", json={})
    assert cancelled.json()["status"] == "cancelled"
    assert _balance(admin_client.app, alice.id) == 0
    listing = admin_client.get("/api/admin/approvals/").json()
    assert listing["pending"] == [] and len(listing["recent"]) == 2


def test_bulk_adjust_queues_large_changes(admin_client, alice, bob):
    _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 1000})
    result = admin_client.post("/api/admin/users/bulk/", json={"user_ids": [alice.id, bob.id], "action": "adjust",
                                                               "amount_cents": 5000}).json()
    assert all(r["ok"] and r.get("pending") for r in result["results"])
    assert len(admin_client.get("/api/admin/approvals/").json()["pending"]) == 2


def test_idempotent_retry_does_not_queue_twice(admin_client, alice):
    _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 100})
    headers = {"Idempotency-Key": "retry-key-0001"}
    for _ in range(2):
        response = admin_client.post(f"/api/admin/users/{alice.id}/balance/",
                                     json={"mode": "adjust", "amount_cents": 500}, headers=headers)
        assert response.status_code == 202
    assert len(admin_client.get("/api/admin/approvals/").json()["pending"]) == 1


def test_claim_credit_above_threshold_needs_approval(admin_client, user_client, alice):
    second = _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 1000})
    claim = _claim(user_client)
    response = admin_client.post(f"/api/admin/claims/{claim['id']}/credit/", json={"credit_cents": 4999})
    assert response.status_code == 202
    assert response.json()["claim"]["status"] == "submitted"
    assert response.json()["claim"]["pending_credit_cents"] == 4999
    duplicate = admin_client.post(f"/api/admin/claims/{claim['id']}/credit/", json={"credit_cents": 4999})
    assert duplicate.status_code == 409

    request_id = response.json()["pending"]["id"]
    assert second.post(f"/api/admin/approvals/{request_id}/approve/").status_code == 200
    detail = admin_client.get(f"/api/admin/claims/{claim['id']}/").json()
    assert detail["status"] == "approved" and detail["credit_cents"] == 4999
    assert _balance(admin_client.app, alice.id) == 4999


def test_closing_a_claim_withdraws_its_pending_credit(admin_client, user_client):
    second = _second_admin(admin_client)
    admin_client.post("/api/admin/settings/", json={"approval_threshold_cents": 1000})
    claim = _claim(user_client)
    pending = admin_client.post(f"/api/admin/claims/{claim['id']}/credit/", json={"credit_cents": 4999}).json()["pending"]
    user_client.post(f"/api/user/claims/{claim['id']}/cancel/", json={})
    assert second.post(f"/api/admin/approvals/{pending['id']}/approve/").status_code == 409
    assert admin_client.get("/api/admin/approvals/").json()["pending"] == []


# --- queues, aging, duplicates, metrics, stale --------------------------------------------


def test_queues_duplicates_and_metrics(admin_client, user_client, bob):
    first = _claim(user_client)
    bob_client = other_client(admin_client)
    bob_client.headers.update(login(bob_client, "bob", USER_PASSWORD))
    second = _claim(bob_client, merchant="northwind outfitters", order_reference="nw-1001")
    _claim(user_client, order_reference="NW-2002", reason="not_received")

    admin_client.post(f"/api/admin/claims/{first['id']}/", json={"assign_to_me": True})
    mine = admin_client.get("/api/admin/claims/?queue=mine").json()
    assert [c["id"] for c in mine["items"]] == [first["id"]]
    assert admin_client.get("/api/admin/claims/?queue=unassigned").json()["total"] == 2
    counts = admin_client.get("/api/admin/claims/queues/").json()
    assert counts["mine"] == 1 and counts["unassigned"] == 2

    listing = {c["id"]: c for c in admin_client.get("/api/admin/claims/").json()["items"]}
    assert listing[first["id"]]["possible_duplicate"] and listing[second["id"]]["possible_duplicate"]
    detail = admin_client.get(f"/api/admin/claims/{first['id']}/").json()
    assert [d["id"] for d in detail["duplicates"]] == [second["id"]]
    assert detail["status_since"]

    admin_client.post(f"/api/admin/claims/{first['id']}/credit/", json={"credit_cents": 1000})
    admin_client.post(f"/api/admin/claims/{second['id']}/", json={"status": "declined"})
    metrics = admin_client.get("/api/admin/claims/metrics/").json()
    assert metrics["approval_rate_percent"] == 50
    assert metrics["credited_count"] == 1 and metrics["credited_cents"] == 1000
    assert {r["reason"]: r["total"] for r in metrics["by_reason"]} == {"damaged": 2, "not_received": 1}
    assert metrics["top_merchants"][0]["total"] == 3


def test_overdue_and_stale_claims(admin_client, user_client):
    claim = _claim(user_client)
    admin_client.post(f"/api/admin/claims/{claim['id']}/", json={"status": "awaiting_customer"})
    assert admin_client.post("/api/admin/claims/close-stale/").json() == {"closed": 0}
    with admin_client.app.state.session_factory() as db:
        old = utcnow() - timedelta(days=20)
        db.execute(update(RefundClaim).where(RefundClaim.id == claim["id"]).values(status_changed_at=old, updated_at=old))
        db.commit()
    row = admin_client.get("/api/admin/claims/").json()["items"][0]
    assert row["overdue"] and row["stale"]
    assert admin_client.get("/api/admin/claims/?queue=stale").json()["total"] == 1
    assert admin_client.post("/api/admin/claims/close-stale/").json() == {"closed": 1}
    mine = user_client.get("/api/user/claims/").json()[0]
    assert mine["status"] == "cancelled"
    assert "did not hear back" in mine["events"][-1]["message"]


# --- saved replies -------------------------------------------------------------------------


def test_saved_replies_crud(admin_client, user_client):
    created = admin_client.post("/api/admin/saved-replies/", json={"title": "Need receipt", "body": "Please upload your receipt."})
    assert created.status_code == 201
    reply_id = created.json()["id"]
    updated = admin_client.post(f"/api/admin/saved-replies/{reply_id}/", json={"title": "Need receipt", "body": "Upload the receipt."})
    assert updated.json()["body"] == "Upload the receipt."
    assert [r["title"] for r in admin_client.get("/api/admin/saved-replies/").json()] == ["Need receipt"]
    assert admin_client.post("/api/admin/saved-replies/", json={"title": "", "body": "x"}).status_code == 422
    assert user_client.get("/api/admin/saved-replies/").status_code == 403
    assert admin_client.delete(f"/api/admin/saved-replies/{reply_id}/").status_code == 200
    assert admin_client.get("/api/admin/saved-replies/").json() == []


# --- account-holder updates, edits, history ------------------------------------------------------


def test_claim_update_badge_and_seen(admin_client, user_client):
    claim = _claim(user_client)
    assert user_client.get("/api/user/claims/").json()[0]["has_update"] is False
    admin_client.post(f"/api/admin/claims/{claim['id']}/", json={"public_reply": "Looking into it.", "internal_note": "Private"})
    assert user_client.get("/api/user/claims/").json()[0]["has_update"] is True
    assert 'aria-label="1 new"' in user_client.get("/dashboard").text
    user_client.post("/api/user/claims/seen/")
    assert user_client.get("/api/user/claims/").json()[0]["has_update"] is False
    admin_client.post(f"/api/admin/claims/{claim['id']}/", json={"internal_note": "Still private"})
    assert user_client.get("/api/user/claims/").json()[0]["has_update"] is False


def test_support_reply_unread_until_seen(admin_client, user_client):
    message = user_client.post("/api/user/messages/", json={"subject": "Hello", "body": "Question"}).json()
    admin_client.post(f"/api/admin/messages/{message['id']}/", json={"reply": "Answer"})
    assert user_client.get("/api/user/messages/").json()[0]["reply_unread"] is True
    user_client.post("/api/user/messages/seen/")
    assert user_client.get("/api/user/messages/").json()[0]["reply_unread"] is False


def test_holder_can_edit_claim_only_when_asked(admin_client, user_client):
    claim = _claim(user_client)
    edit = {**CLAIM, "amount_cents": 5999, "order_reference": "NW-1001-B"}
    assert user_client.post(f"/api/user/claims/{claim['id']}/", json=edit).status_code == 422
    admin_client.post(f"/api/admin/claims/{claim['id']}/", json={"status": "awaiting_customer"})
    edited = user_client.post(f"/api/user/claims/{claim['id']}/", json=edit)
    assert edited.status_code == 200, edited.text
    assert edited.json()["amount_cents"] == 5999
    assert "Amount: $49.99 → $59.99" in edited.json()["events"][-1]["message"]
    assert user_client.post(f"/api/user/claims/{claim['id']}/", json=edit).status_code == 422  # nothing changed


def test_holder_history_csv(admin_client, user_client, alice):
    admin_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "adjust", "amount_cents": 1234, "reason": "=SUM(A1)"})
    response = user_client.get("/api/user/activity/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("id,created_at_utc,type")
    assert "12.34" in lines[1] and "'=SUM(A1)" in lines[1]
    assert admin_client.get("/api/user/activity/export.csv").status_code == 403


# --- recovery codes, closing, sign-in history ------------------------------------------------


def test_recovery_codes_reset_password_once(app_client, user_client):
    assert user_client.post("/api/user/recovery-codes/", json={"password": "wrong"}).status_code == 422
    codes = user_client.post("/api/user/recovery-codes/", json={"password": USER_PASSWORD}).json()["codes"]
    assert len(codes) == 8 and len(set(codes)) == 8
    assert user_client.get("/api/user/recovery-codes/").json() == {"remaining": 8}

    browser = other_client(app_client)
    page = browser.get("/forgot-password")
    assert page.status_code == 200
    token = page_csrf(page.text)
    form = {"username": "alice", "recovery_code": codes[0].lower(), "new_password": "brand-new-pass-1",
            "confirm_password": "brand-new-pass-1", "csrf_token": token}
    done = browser.post("/forgot-password", data=form, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/login?password_changed=1"
    assert api_login(other_client(app_client), "alice", "brand-new-pass-1").status_code == 200
    # The old session was signed out and the code is spent.
    assert user_client.get("/api/me/").status_code == 401
    browser = other_client(app_client)
    token = page_csrf(browser.get("/forgot-password").text)
    reused = browser.post("/forgot-password", data={**form, "csrf_token": token, "new_password": "another-pass-12",
                                                    "confirm_password": "another-pass-12"})
    assert reused.status_code == 422 and "don&#39;t match" in reused.text


def test_recovery_not_available_for_admins(app_client):
    browser = other_client(app_client)
    token = page_csrf(browser.get("/forgot-password").text)
    response = browser.post("/forgot-password", data={"username": ADMIN_USERNAME, "recovery_code": "AAAA-AAAA-AAAA-AAAA",
                                                      "new_password": "x" * 12, "confirm_password": "x" * 12,
                                                      "csrf_token": token})
    assert response.status_code == 422


def test_holder_closes_own_account(admin_client, user_client, alice):
    assert user_client.post("/api/user/close/", json={"password": "wrong"}).status_code == 422
    closed = user_client.post("/api/user/close/", json={"password": USER_PASSWORD})
    assert closed.json()["redirect"] == "/login?closed=1"
    assert user_client.get("/api/me/").status_code == 401
    assert api_login(other_client(admin_client), "alice", USER_PASSWORD).status_code == 403
    actions = [e["action"] for e in admin_client.get("/api/admin/audit/?action=account.closed").json()["items"]]
    assert actions == ["account.closed"]
    assert admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "active"}).status_code == 200


def test_sign_in_history(user_client):
    history = user_client.get("/api/user/sign-ins/").json()
    assert len(history) == 1 and history[0]["ip"]


# --- administrator two-step sign-in ----------------------------------------------------------


def _enable_totp(client) -> str:
    secret = client.post("/api/admin/security/totp/setup/").json()["secret"]
    assert client.post("/api/admin/security/totp/enable/", json={"code": "000000"}).status_code == 422
    enabled = client.post("/api/admin/security/totp/enable/", json={"code": totp_code(secret)})
    assert enabled.status_code == 200, enabled.text
    return secret


def test_admin_two_step_sign_in(app_client, admin_client):
    secret = _enable_totp(admin_client)
    assert admin_client.get("/api/admin/security/").json() == {"totp_enabled": True}

    browser = other_client(app_client)
    first = api_login(browser, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert first.status_code == 202 and first.json()["two_factor_required"]
    headers = {"X-CSRF-Token": first.json()["csrf_token"]}
    assert browser.get("/api/me/").status_code == 401  # not signed in yet
    assert browser.post("/api/login/code/", json={"code": "123456"}, headers=headers).status_code == 401
    # The code used at enrolment has been spent; use the next time step.
    import time
    code = totp_code(secret, time.time() + 30)
    ok = browser.post("/api/login/code/", json={"code": code}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert browser.get("/api/me/").json()["username"] == ADMIN_USERNAME
    replay = other_client(app_client)
    again = api_login(replay, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert replay.post("/api/login/code/", json={"code": code},
                       headers={"X-CSRF-Token": again.json()["csrf_token"]}).status_code == 401


def test_admin_two_step_form_flow_and_reset(app_client, admin_client):
    _enable_totp(admin_client)
    browser = other_client(app_client)
    token = page_csrf(browser.get("/login").text)
    step = browser.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "csrf_token": token},
                        follow_redirects=False)
    assert step.headers["location"] == "/login/code"
    page = browser.get("/login/code")
    assert page.status_code == 200 and "smartsupp" not in page.text
    wrong = browser.post("/login/code", data={"code": "000000", "csrf_token": page_csrf(page.text)})
    assert wrong.status_code == 401

    second = _second_admin(admin_client)
    with app_client.app.state.session_factory() as db:
        admin_id = db.scalar(select(User.id).where(User.username == ADMIN_USERNAME))
    assert second.post(f"/api/admin/users/{admin_id}/totp-reset/").status_code == 200
    assert api_login(other_client(app_client), ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200


def test_totp_is_admin_only(user_client):
    assert user_client.post("/api/admin/security/totp/setup/").status_code == 403


def test_new_pages_render(admin_client, user_client):
    for path in ("/admin/approvals", "/admin/security", "/admin/claims", "/admin/settings"):
        assert admin_client.get(path).status_code == 200, path
    for path in ("/dashboard", "/dashboard/claims", "/dashboard/support", "/dashboard/profile", "/dashboard/activity"):
        assert user_client.get(path).status_code == 200, path
