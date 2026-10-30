import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.bootstrap import ensure_admin
from backend.config import ConfigError, Settings
from backend.main import create_app
from backend.models import User
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, USER_PASSWORD, adjust_balance, api_login, login, make_user


def test_data_survives_restart(make_settings):
    settings = make_settings()
    with TestClient(create_app(settings)) as first:
        first.headers.update(login(first, ADMIN_USERNAME, ADMIN_PASSWORD))
        user_id = make_user(first, "persist.me").id
        adjust_balance(first, user_id, 4_321, reason="before restart")

    # A brand-new app instance on the same database file, with a *different*
    # bootstrap password: existing data and the existing admin must be untouched.
    restarted = Settings(**{**settings.__dict__, "admin_password": "a-different-password-999"})
    with TestClient(create_app(restarted)) as second:
        assert api_login(second, ADMIN_USERNAME, "a-different-password-999").status_code == 401
        second.headers.update(login(second, ADMIN_USERNAME, ADMIN_PASSWORD))
        detail = second.get(f"/api/admin/users/{user_id}/").json()
        assert detail["user"]["balance_cents"] == 4_321
        assert detail["activity"]["total"] == 1
        assert detail["activity"]["items"][0]["reason"] == "before restart"
        assert api_login(TestClient(second.app), "persist.me", USER_PASSWORD).status_code == 200
        with second.app.state.session_factory() as db:
            assert db.scalar(select(User).where(User.role == "admin")).username == ADMIN_USERNAME


def test_bootstrap_requires_credentials_when_no_admin(make_settings):
    app = create_app(make_settings(admin_username=None, admin_password=None))
    with pytest.raises(ConfigError, match="ADMIN_USERNAME"):
        with TestClient(app):
            pass


def test_bootstrap_rejects_placeholder_and_weak_passwords(make_settings):
    for password in ("replace-with-a-strong-local-password", "short"):
        settings = make_settings(admin_password=password)
        app = create_app(settings)
        with pytest.raises(ConfigError):
            with TestClient(app):
                pass


def test_bootstrap_does_not_need_credentials_once_admin_exists(make_settings):
    settings = make_settings()
    with TestClient(create_app(settings)):
        pass
    with TestClient(create_app(make_settings(admin_username=None, admin_password=None))) as client:
        assert client.get("/login").status_code == 200


def test_secret_key_validation():
    with pytest.raises(ConfigError):
        Settings(secret_key="replace-with-a-long-random-secret").validate()
    with pytest.raises(ConfigError):
        Settings(secret_key="too-short").validate()


def test_ensure_admin_is_idempotent(make_settings):
    settings = make_settings()
    app = create_app(settings)
    with TestClient(app):
        with app.state.session_factory() as db:
            ensure_admin(db, settings)
            ensure_admin(db, settings)
            assert db.query(User).filter(User.role == "admin").count() == 1
