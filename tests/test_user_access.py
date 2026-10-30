from backend.main import SESSION_COOKIE
from tests.conftest import USER_PASSWORD, adjust_balance, login, other_client


def test_user_sees_own_dashboard(admin_client, user_client, alice):
    adjust_balance(admin_client, alice.id, 12345)
    response = user_client.get("/dashboard")
    assert response.status_code == 200
    assert "$123.45" in response.text
    assert "alice" in response.text


def test_user_sees_own_activity_only(admin_client, user_client, alice, bob):
    adjust_balance(admin_client, alice.id, 1000, reason="alice credit")
    adjust_balance(admin_client, bob.id, 5000, reason="bob credit")
    data = user_client.get("/api/user/activity/").json()
    assert data["total"] == 1
    assert data["items"][0]["reason"] == "alice credit"
    assert data["items"][0]["amount_cents"] == 1000
    assert "admin_id" not in data["items"][0]


def test_user_activity_ignores_client_supplied_user_id(admin_client, user_client, alice, bob):
    adjust_balance(admin_client, bob.id, 5000, reason="bob credit")
    data = user_client.get(f"/api/user/activity/?user_id={bob.id}").json()
    assert data["total"] == 0


def test_user_activity_pagination_and_limits(admin_client, user_client, alice):
    for i in range(1, 6):
        adjust_balance(admin_client, alice.id, i * 100)
    page = user_client.get("/api/user/activity/?page=2&page_size=2").json()
    assert page["total"] == 5 and page["pages"] == 3 and len(page["items"]) == 2
    # newest first: page 1 = 500, 400; page 2 = 300, 200
    assert [a["amount_cents"] for a in page["items"]] == [300, 200]
    assert user_client.get("/api/user/activity/?page_size=500").status_code == 422
    assert user_client.get("/api/user/activity/?page=0").status_code == 422


def test_user_cannot_access_admin_pages(user_client, bob):
    assert user_client.get("/admin").status_code == 403
    assert user_client.get(f"/admin/users/{bob.id}").status_code == 403


def test_user_cannot_call_admin_apis(user_client, alice, bob):
    for path in ("/api/admin/stats/", "/api/admin/users/", f"/api/admin/users/{bob.id}/", f"/api/admin/users/{bob.id}/activity/"):
        response = user_client.get(path)
        assert response.status_code == 403, path
    assert user_client.post("/api/admin/users/", json={"username": "mallory", "password": "password-123"}).status_code == 403


def test_user_cannot_change_own_balance_or_status(user_client, alice):
    response = user_client.post(f"/api/admin/users/{alice.id}/balance/", json={"mode": "set", "target_cents": 10**9})
    assert response.status_code == 403
    response = user_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "active"})
    assert response.status_code == 403
    assert user_client.get("/api/me/").json()["balance_cents"] == 0


def test_tampered_cookie_does_not_grant_access(app_client, alice):
    client = other_client(app_client)
    login(client, "alice", USER_PASSWORD)
    cookie = client.cookies.get(SESSION_COOKIE)
    # Flip characters in the signed payload: the signature no longer matches.
    tampered = cookie[:-4] + ("AAAA" if not cookie.endswith("AAAA") else "BBBB")
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, tampered)
    assert client.get("/api/me/").status_code == 401
    # A cookie forged with another key is rejected too.
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, "eyJ1aWQiOiAxLCAic3YiOiAxfQ==.forged.signature")
    assert client.get("/api/admin/stats/").status_code == 401


def test_role_comes_from_database_not_request(user_client, alice):
    # Extra role fields in a login payload are rejected outright.
    response = user_client.post("/api/login/", json={"username": "alice", "password": USER_PASSWORD, "role": "admin"})
    assert response.status_code == 422
    assert user_client.get("/api/me/").json()["role"] == "user"
    assert user_client.get("/api/admin/stats/?role=admin").status_code == 403


def test_admin_dashboard_redirects_admin_away_from_user_dashboard(admin_client):
    response = admin_client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    # The account-holder activity API is for account holders only.
    assert admin_client.get("/api/user/activity/").status_code == 403


def test_deactivated_user_session_stops_working(admin_client, user_client, alice):
    assert user_client.get("/api/me/").status_code == 200
    assert admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "inactive"}).status_code == 200
    assert user_client.get("/api/me/").status_code == 401
    assert user_client.get("/api/user/activity/").status_code == 401
    assert user_client.get("/dashboard", follow_redirects=False).status_code == 303
    # Reactivation does not revive the old session (session version was bumped).
    assert admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "active"}).status_code == 200
    assert user_client.get("/api/me/").status_code == 401


def test_csrf_required_for_state_changes(admin_client, alice):
    response = admin_client.post(
        f"/api/admin/users/{alice.id}/balance/",
        json={"mode": "adjust", "amount_cents": 100},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert admin_client.get(f"/api/admin/users/{alice.id}/").json()["activity"]["total"] == 0
