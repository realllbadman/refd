"""Test fixtures. Every test gets its own temporary SQLite database; the real
.env and ledger.db are never read or modified."""

from __future__ import annotations

import os
import re

# Must happen before any backend import (config reads the environment at import).
os.environ["LEDGER_SKIP_DOTENV"] = "1"
os.environ["SECRET_KEY"] = "test-secret-key-" + "x" * 40
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-password-123"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

from backend import security  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.main import create_app  # noqa: E402
from backend.services.account_service import create_account  # noqa: E402

# Cheap hashes keep the suite fast; production uses 12 rounds.
security._pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password-123"
USER_PASSWORD = "user-password-123"
CSRF_META = re.compile(r'name="csrf-token" content="([^"]+)"')


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides) -> Settings:
        values = dict(
            secret_key="test-secret-key-" + "x" * 40,
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            claim_upload_dir=tmp_path / "uploads",
            admin_username=ADMIN_USERNAME,
            admin_password=ADMIN_PASSWORD,
            allow_negative=False,
            login_rate_limit=5,
            login_rate_window_seconds=60,
        )
        values.update(overrides)
        settings = Settings(**values)
        settings.validate()
        return settings

    return _make


@pytest.fixture
def make_client(make_settings):
    clients = []

    def _make(**overrides) -> TestClient:
        app = create_app(make_settings(**overrides))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def app_client(make_client):
    return make_client()


@pytest.fixture
def anon(app_client):
    return app_client


def csrf_token(client: TestClient) -> str:
    return client.get("/api/csrf/").json()["csrf_token"]


def api_login(client: TestClient, username: str, password: str):
    token = csrf_token(client)
    return client.post("/api/login/", json={"username": username, "password": password}, headers={"X-CSRF-Token": token})


def login(client: TestClient, username: str, password: str) -> dict:
    """Log in and return headers carrying the session's CSRF token."""
    response = api_login(client, username, password)
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def make_user(client: TestClient, username: str, password: str = USER_PASSWORD, role: str = "user"):
    with client.app.state.session_factory() as db:
        return create_account(db, username=username, password=password, role=role).user


def other_client(client: TestClient) -> TestClient:
    """A second browser (separate cookie jar) against the same app."""
    second = TestClient(client.app)
    return second


@pytest.fixture
def admin_client(app_client):
    app_client.headers.update(login(app_client, ADMIN_USERNAME, ADMIN_PASSWORD))
    return app_client


@pytest.fixture
def alice(app_client):
    return make_user(app_client, "alice")


@pytest.fixture
def bob(app_client):
    return make_user(app_client, "bob")


@pytest.fixture
def user_client(app_client, alice):
    """A separate browser session logged in as alice."""
    client = other_client(app_client)
    client.headers.update(login(client, "alice", USER_PASSWORD))
    return client


def set_balance(admin: TestClient, user_id: int, cents: int, **extra):
    return admin.post(f"/api/admin/users/{user_id}/balance/", json={"mode": "set", "target_cents": cents, **extra})


def adjust_balance(admin: TestClient, user_id: int, cents: int, headers=None, **extra):
    return admin.post(
        f"/api/admin/users/{user_id}/balance/", json={"mode": "adjust", "amount_cents": cents, **extra}, headers=headers
    )


def page_csrf(html: str) -> str:
    return CSRF_META.search(html).group(1)
