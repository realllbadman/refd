"""Authentication / authorisation dependencies shared by pages and API routes.

The session cookie only carries a user id, a session version and a CSRF token.
Role and status are always re-read from the database on every request.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.database import get_db
from backend.errors import Forbidden, LoginRequired, NotAuthenticated, PageForbidden
from backend.models import User
from backend.security import (
    CSRF_HEADER,
    SESSION_USER_KEY,
    SESSION_VERSION_KEY,
    csrf_token_valid,
    end_session,
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_current_user(request: Request, db: Session) -> User | None:
    """Return the active account bound to this session, or ``None``.

    A session whose account no longer exists, was deactivated, or whose
    session version was bumped is cleared.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not isinstance(user_id, int):
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active or request.session.get(SESSION_VERSION_KEY) != user.session_version:
        end_session(request)
        request.state.session_expired = True
        return None
    # Plain snapshot for error pages, which render after the DB session has closed.
    request.state.current_user = SimpleNamespace(id=user.id, username=user.username, is_admin=user.is_admin)
    return user


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    return get_current_user(request, db)


# --- API ----------------------------------------------------------------------


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Any authenticated, active account (user or admin)."""
    user = get_current_user(request, db)
    if user is None:
        raise NotAuthenticated("Your session has ended. Please sign in again.")
    return user


def require_customer(user: User = Depends(require_user)) -> User:
    if user.is_admin:
        raise Forbidden("This endpoint is only available to account holders.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Administrator privileges required.")
    return user


def verify_csrf(request: Request) -> None:
    """For cookie-authenticated state-changing API requests."""
    if not csrf_token_valid(request, request.headers.get(CSRF_HEADER)):
        raise Forbidden("Missing or invalid CSRF token. Reload the page and try again.", code="csrf_failed")


def require_admin_write(request: Request, admin: User = Depends(require_admin)) -> User:
    """Administrator check first (401/403), then CSRF validation."""
    verify_csrf(request)
    return admin


# --- pages --------------------------------------------------------------------


def page_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired(expired=getattr(request.state, "session_expired", False))
    return user


def page_admin(user: User = Depends(page_user)) -> User:
    if not user.is_admin:
        raise PageForbidden()
    return user
