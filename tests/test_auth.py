from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    api_login,
    csrf_token,
    login,
    make_user,
    page_csrf,
)


def test_login_page_renders(anon):
    response = anon.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert 'href="/register"' in response.text


def test_api_login_success_returns_safe_summary(anon, alice):
    response = api_login(anon, "alice", USER_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "user"
    assert body["redirect"] == "/dashboard"
    assert "password_hash" not in response.text
    assert anon.get("/api/me/").json()["username"] == "alice"


def test_login_normalises_username(anon, alice):
    assert api_login(anon, "  ALICE ", USER_PASSWORD).status_code == 200


def test_admin_login_redirects_to_admin(anon):
    response = api_login(anon, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert response.status_code == 200
    assert response.json()["redirect"] == "/admin"


def test_form_login_and_role_redirect(anon, alice):
    token = page_csrf(anon.get("/login").text)
    response = anon.post(
        "/login", data={"username": "alice", "password": USER_PASSWORD, "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_invalid_credentials_are_generic(anon, alice):
    wrong_password = api_login(anon, "alice", "not-the-password")
    unknown_user = api_login(anon, "nobody", "not-the-password")
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]
    assert anon.get("/api/me/").status_code == 401


def test_form_login_invalid_credentials(anon, alice):
    token = page_csrf(anon.get("/login").text)
    response = anon.post("/login", data={"username": "alice", "password": "wrong-one", "csrf_token": token})
    assert response.status_code == 401
    assert "Incorrect username or password" in response.text


def test_login_requires_csrf(anon, alice):
    response = anon.post("/api/login/", json={"username": "alice", "password": USER_PASSWORD})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    form = anon.post("/login", data={"username": "alice", "password": USER_PASSWORD, "csrf_token": "bogus"})
    assert form.status_code == 400
    assert anon.get("/api/me/").status_code == 401


def test_overlong_password_rejected_not_truncated(anon):
    long_password = "a" * 73
    response = api_login(anon, "alice", long_password)
    assert response.status_code == 422


def test_inactive_account_cannot_log_in(admin_client, alice):
    assert admin_client.post(f"/api/admin/users/{alice.id}/status/", json={"status": "inactive"}).status_code == 200
    fresh = admin_client.__class__(admin_client.app)
    response = api_login(fresh, "alice", USER_PASSWORD)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_unavailable"
    assert fresh.get("/api/me/").status_code == 401


def test_logout_clears_session(anon, alice):
    headers = login(anon, "alice", USER_PASSWORD)
    assert anon.get("/api/me/").status_code == 200
    assert anon.post("/api/logout/", headers=headers).status_code == 200
    assert anon.get("/api/me/").status_code == 401
    assert anon.get("/dashboard", follow_redirects=False).status_code == 303


def test_form_logout_requires_post_and_csrf(anon, alice):
    login(anon, "alice", USER_PASSWORD)
    # GET only shows a confirmation page; it never signs out.
    assert anon.get("/logout").status_code == 200
    assert anon.get("/api/me/").status_code == 200
    # Forged POST without the token does not sign out.
    anon.post("/logout", data={}, follow_redirects=False)
    assert anon.get("/api/me/").status_code == 200
    token = page_csrf(anon.get("/dashboard").text)
    response = anon.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert anon.get("/api/me/").status_code == 401


def test_unauthenticated_pages_redirect_to_login(anon):
    for path in ("/dashboard", "/admin", "/admin/users/1"):
        response = anon.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


def test_unauthenticated_apis_return_401(anon):
    for path in ("/api/me/", "/api/user/activity/", "/api/admin/stats/", "/api/admin/users/", "/api/admin/users/1/"):
        response = anon.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "not_authenticated"
    token = csrf_token(anon)
    for path, body in (
        ("/api/admin/users/", {"username": "eve", "password": "password-123"}),
        ("/api/admin/users/1/status/", {"status": "inactive"}),
        ("/api/admin/users/1/balance/", {"mode": "adjust", "amount_cents": 100}),
    ):
        assert anon.post(path, json=body, headers={"X-CSRF-Token": token}).status_code == 401, path


def test_login_rate_limit(anon, alice):
    # make_settings uses a limit of 5 failures per window.
    for _ in range(5):
        assert api_login(anon, "alice", "wrong-password").status_code == 401
    blocked = api_login(anon, "alice", USER_PASSWORD)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    # Same response whether or not the username exists.
    assert api_login(anon, "ghost", "wrong-password").status_code == 429
    anon.app.state.login_limiter.reset()
    assert api_login(anon, "alice", USER_PASSWORD).status_code == 200


def test_safe_next_redirect(anon, alice):
    token = page_csrf(anon.get("/login").text)
    response = anon.post(
        "/login",
        data={"username": "alice", "password": USER_PASSWORD, "csrf_token": token, "next": "//evil.example/x"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/dashboard"


def test_security_headers(anon):
    response = anon.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_new_admin_created_by_make_user_can_log_in(anon):
    make_user(anon, "second.admin", "second-admin-pass", role="admin")
    assert api_login(anon, "second.admin", "second-admin-pass").json()["redirect"] == "/admin"
