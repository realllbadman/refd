from sqlalchemy import select

from backend.models import BalanceActivity, User
from tests.conftest import ADMIN_USERNAME, USER_PASSWORD, adjust_balance, api_login, make_user, other_client


def _db(client):
    return client.app.state.session_factory()


# --- account creation -------------------------------------------------------------


def test_admin_creates_account_with_zero_balance(admin_client):
    response = admin_client.post("/api/admin/users/", json={"username": "New.User", "password": "temp-pass-123"})
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["username"] == "new.user"
    assert body["user"]["balance_cents"] == 0
    assert body["user"]["role"] == "user"
    assert body["user"]["status"] == "active"
    assert body["temporary_password"] is None
    assert "password" not in response.text.replace("temporary_password", "")
    assert "$2b$" not in response.text

    with _db(admin_client) as db:
        user = db.scalar(select(User).where(User.username == "new.user"))
        assert user.balance_cents == 0
        assert user.password_hash.startswith("$2b$")
        assert user.password_hash != "temp-pass-123"

    # The new account can log in with the chosen password.
    assert api_login(other_client(admin_client), "new.user", "temp-pass-123").status_code == 200


def test_admin_creates_account_with_generated_password(admin_client):
    response = admin_client.post("/api/admin/users/", json={"username": "gen.user", "generate_password": True})
    assert response.status_code == 201
    temp = response.json()["temporary_password"]
    assert temp and len(temp) >= 12
    assert api_login(other_client(admin_client), "gen.user", temp).status_code == 200
    # Listing the account later never reveals the password.
    assert temp not in admin_client.get("/api/admin/users/").text


def test_duplicate_username_rejected(admin_client):
    assert admin_client.post("/api/admin/users/", json={"username": "dupe", "password": "temp-pass-123"}).status_code == 201
    response = admin_client.post("/api/admin/users/", json={"username": "DUPE ", "password": "temp-pass-123"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


def test_invalid_usernames_and_passwords_rejected(admin_client):
    bad = [
        {"username": "ab", "password": "temp-pass-123"},
        {"username": "has space", "password": "temp-pass-123"},
        {"username": "-leading", "password": "temp-pass-123"},
        {"username": "<script>", "password": "temp-pass-123"},
        {"username": "valid.name", "password": "short"},
        {"username": "valid.name", "password": "x" * 73},
        {"username": "valid.name", "password": "é" * 40},  # 80 bytes in UTF-8
        {"username": "valid.name"},
        {"username": "valid.name", "password": "temp-pass-123", "generate_password": True},
    ]
    for payload in bad:
        response = admin_client.post("/api/admin/users/", json=payload)
        assert response.status_code == 422, payload


def test_create_cannot_set_role_status_or_balance(admin_client):
    for extra in ({"role": "admin"}, {"status": "inactive"}, {"balance_cents": 10_000}):
        payload = {"username": "sneaky", "password": "temp-pass-123", **extra}
        assert admin_client.post("/api/admin/users/", json=payload).status_code == 422, extra
    with _db(admin_client) as db:
        assert db.scalar(select(User).where(User.username == "sneaky")) is None


def test_validation_errors_do_not_echo_passwords(admin_client):
    response = admin_client.post("/api/admin/users/", json={"username": "x", "password": "my-secret-password-1"})
    assert "my-secret-password-1" not in response.text


def test_no_json_registration_endpoint(anon):
    # Self-registration exists only as the CSRF-protected HTML form at /register.
    assert anon.post("/api/register/", json={}).status_code in (404, 405)


# --- listing, search, detail, stats ------------------------------------------------


def test_list_search_and_pagination(admin_client):
    ids = [make_user(admin_client, f"member{i:02d}").id for i in range(25)]
    page1 = admin_client.get("/api/admin/users/?page=1&page_size=10").json()
    assert page1["total"] == 26  # 25 members + admin
    assert len(page1["items"]) == 10 and page1["pages"] == 3
    assert "password_hash" not in str(page1)
    page3 = admin_client.get("/api/admin/users/?page=3&page_size=10").json()
    assert len(page3["items"]) == 6

    by_name = admin_client.get("/api/admin/users/?q=member0").json()
    assert by_name["total"] == 10
    by_id = admin_client.get(f"/api/admin/users/?q={ids[3]}").json()
    assert any(u["id"] == ids[3] for u in by_id["items"])
    assert admin_client.get("/api/admin/users/?q=%25").json()["total"] == 0  # LIKE wildcards are escaped
    assert admin_client.get("/api/admin/users/?q=zzz-none").json()["items"] == []
    assert admin_client.get("/api/admin/users/?page_size=1000").status_code == 422


def test_user_detail_and_404(admin_client, alice):
    adjust_balance(admin_client, alice.id, 2500, reason="welcome")
    detail = admin_client.get(f"/api/admin/users/{alice.id}/").json()
    assert detail["user"]["username"] == "alice"
    assert detail["user"]["balance_display"] == "$25.00"
    assert detail["activity"]["items"][0]["admin_username"] == ADMIN_USERNAME
    assert admin_client.get("/api/admin/users/99999/").status_code == 404
    assert admin_client.get("/admin/users/99999").status_code == 404
    page = admin_client.get(f"/admin/users/{alice.id}")
    assert page.status_code == 200 and "$25.00" in page.text


def test_stats_from_database(admin_client, alice, bob):
    adjust_balance(admin_client, alice.id, 10_000)
    adjust_balance(admin_client, bob.id, 2_550)
    admin_client.post(f"/api/admin/users/{bob.id}/status/", json={"status": "inactive"})
    stats = admin_client.get("/api/admin/stats/").json()
    assert stats["total_users"] == 2
    assert stats["active_users"] == 1
    assert stats["inactive_users"] == 1
    assert stats["total_balance_cents"] == 12_550
    assert stats["total_balance_display"] == "$125.50"
    assert stats["recent_activity_count"] == 2


def test_admin_pages_render(admin_client, alice):
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "New account" in response.text


# --- account status ---------------------------------------------------------------------


def test_deactivate_keeps_balance_and_history(admin_client, alice):
    adjust_balance(admin_client, alice.id, 4_200)
    response = admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "inactive"})
    assert response.status_code == 200 and response.json()["status"] == "inactive"
    detail = admin_client.get(f"/api/admin/users/{alice.id}/").json()
    assert detail["user"]["balance_cents"] == 4_200
    assert detail["activity"]["total"] == 1

    response = admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "active"})
    assert response.json()["status"] == "active"
    assert api_login(other_client(admin_client), "alice", USER_PASSWORD).status_code == 200


def test_invalid_status_rejected(admin_client, alice):
    assert admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "deleted"}).status_code == 422
    assert admin_client.post("/api/admin/users/99999/status/", json={"status": "inactive"}).status_code == 404


def test_cannot_deactivate_self_or_last_admin(admin_client):
    with _db(admin_client) as db:
        admin_id = db.scalar(select(User.id).where(User.username == ADMIN_USERNAME))
    response = admin_client.post(f"/api/admin/users/{admin_id}/status/", json={"status": "inactive"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "self_deactivation"

    second = make_user(admin_client, "second.admin", "second-admin-pass", role="admin")
    # Deactivating another admin is fine while one active admin remains.
    assert admin_client.post(f"/api/admin/users/{second.id}/status/", json={"status": "inactive"}).status_code == 200
    with _db(admin_client) as db:
        assert db.get(User, admin_id).status == "active"


def test_last_admin_guard_in_service(admin_client):
    from backend.errors import Conflict
    from backend.services.account_service import set_status

    second = make_user(admin_client, "second.admin", "second-admin-pass", role="admin")
    with _db(admin_client) as db:
        first = db.scalar(select(User).where(User.username == ADMIN_USERNAME))
        # Simulate "first" being the only active admin acting on another admin that is
        # also the last one: deactivate second first, then try to deactivate first via second.
        set_status(db, actor=first, user_id=second.id, status="inactive")
        second_obj = db.get(User, second.id)
        try:
            set_status(db, actor=second_obj, user_id=first.id, status="inactive")
            raise AssertionError("expected Conflict")
        except Conflict as exc:
            assert exc.code == "last_admin"


def test_activity_is_append_only_at_database_level(admin_client, alice):
    adjust_balance(admin_client, alice.id, 100)
    import sqlalchemy

    with _db(admin_client) as db:
        activity = db.scalar(select(BalanceActivity))
        for statement in ("UPDATE balance_activity SET amount_cents = 1", "DELETE FROM balance_activity"):
            try:
                db.execute(sqlalchemy.text(statement))
                db.commit()
                raise AssertionError("trigger did not fire")
            except sqlalchemy.exc.DatabaseError as exc:
                db.rollback()
                assert "append-only" in str(exc)
        assert db.get(BalanceActivity, activity.id).amount_cents == 100


def test_no_endpoint_edits_or_deletes_activity(admin_client, alice):
    adjust_balance(admin_client, alice.id, 100)
    activity_id = admin_client.get(f"/api/admin/users/{alice.id}/").json()["activity"]["items"][0]["id"]
    for method in ("put", "patch", "delete"):
        response = getattr(admin_client, method)(f"/api/admin/users/{alice.id}/activity/")
        assert response.status_code in (404, 405)
        response = admin_client.request(method.upper(), f"/api/admin/activity/{activity_id}/")
        assert response.status_code in (404, 405)
