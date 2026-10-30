"""Startup provisioning: initial administrator and optional demo accounts."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import PLACEHOLDER_PREFIX, ConfigError, Settings
from backend.errors import AppError
from backend.models import ROLE_ADMIN, User
from backend.security import PasswordPolicyError, validate_new_password
from backend.services.account_service import create_account, find_by_username, validate_username

logger = logging.getLogger("ledger")

MIN_ADMIN_PASSWORD_LENGTH = 12
DEMO_USERNAMES = ("demo.avery", "demo.jordan", "demo.riley")


def ensure_admin(db: Session, settings: Settings) -> None:
    """Create the first administrator if none exists. Never modifies an existing one."""
    if db.scalar(select(User.id).where(User.role == ROLE_ADMIN).limit(1)) is not None:
        return

    username, password = settings.admin_username, settings.admin_password
    if not username or not password:
        raise ConfigError(
            "No administrator exists yet. Set ADMIN_USERNAME and ADMIN_PASSWORD in .env "
            "(see .env.example) and restart to create the initial administrator."
        )
    if password.startswith(PLACEHOLDER_PREFIX):
        raise ConfigError("ADMIN_PASSWORD still has the example placeholder value. Choose a strong password in .env.")
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise ConfigError(f"ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")
    try:
        validate_username(username)
        validate_new_password(password)
    except (AppError, PasswordPolicyError) as exc:
        raise ConfigError(f"Invalid bootstrap administrator credentials: {getattr(exc, 'message', exc)}") from exc
    if find_by_username(db, username) is not None:
        raise ConfigError(
            f"ADMIN_USERNAME {username!r} is already used by a non-administrator account. Choose another name."
        )

    created = create_account(db, username=username, password=password, role=ROLE_ADMIN)
    logger.info("Initial administrator %r created (id=%s).", created.user.username, created.user.id)


def seed_demo_accounts(db: Session) -> None:
    """Create fictional zero-balance accounts that do not exist yet (SEED_DEMO=1)."""
    for username in DEMO_USERNAMES:
        if find_by_username(db, username) is not None:
            continue
        created = create_account(db, username=username, password=None, generate_password=True)
        # Printed once, to the local console only, for this one-time setup.
        print(f"[seed] created demo account {username!r} with temporary password: {created.temporary_password}")
