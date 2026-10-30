"""Login / logout pages and authentication API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import current_user_optional, require_user, verify_csrf
from backend.errors import AppError, Forbidden, NotAuthenticated, RateLimited, ValidationFailed
from backend.models import User
from backend.schemas import LoginCodeRequest, LoginRequest, LoginResponse, OkResponse, UserSummary
from backend.security import (
    CSRF_FORM_FIELD,
    begin_two_factor,
    client_ip,
    csrf_token_valid,
    end_session,
    get_csrf_token,
    password_too_long,
    pending_two_factor,
    start_session,
    verify_password,
)
from backend.services import account_service
from backend.services.account_service import find_by_username, record_login
from backend.services.settings_service import get_all as get_settings_values
from backend.templating import render

logger = logging.getLogger("ledger")
router = APIRouter()

INVALID_CREDENTIALS = "Incorrect username or password."
INVALID_CODE = "That code didn't match. Enter the current 6-digit code from your authenticator app."
CODE_EXPIRED = "Your sign-in timed out. Please enter your password again."
ACCOUNT_UNAVAILABLE = "This account is unavailable. Please contact your administrator."


def public_context(db: Session) -> dict:
    values = get_settings_values(db)
    return {"registration_open": values["registration_open"], "public_notice": values["public_notice"]}


def home_for(user: User) -> str:
    return "/admin" if user.is_admin else "/dashboard"


def safe_next(target: str | None, user: User) -> str:
    """Only allow same-site relative paths that the user's role can open."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return home_for(user)
    path = target.split("?", 1)[0]
    if path.startswith("/admin") and not user.is_admin:
        return home_for(user)
    if path.startswith("/dashboard") and user.is_admin:
        return home_for(user)
    if path in ("/login", "/login/code", "/logout", "/register", "/forgot-password"):
        return home_for(user)
    return target


def authenticate(request: Request, db: Session, username: str, password: str) -> User:
    limiter = request.app.state.login_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        raise RateLimited("Too many failed sign-in attempts. Please wait and try again.", limiter.retry_after(ip))
    if not username.strip() or not password:
        raise ValidationFailed("Enter your username and password.")
    if password_too_long(password):
        raise ValidationFailed("Password is too long (maximum 72 bytes).")

    user = find_by_username(db, username)
    if not verify_password(password, user.password_hash if user else None):
        limiter.record_failure(ip)
        logger.info("Failed login from %s", ip)
        raise NotAuthenticated(INVALID_CREDENTIALS, code="invalid_credentials")
    if not user.is_active:
        # Only revealed after the correct password has been supplied.
        raise Forbidden(ACCOUNT_UNAVAILABLE, code="account_unavailable")
    return user


def finish_sign_in(request: Request, db: Session, user: User) -> str:
    """Record the sign-in and open the session; returns the new CSRF token."""
    record_login(db, user, client_ip(request))
    logger.info("User %s signed in", user.id)
    return start_session(request, user)


def verify_second_step(request: Request, db: Session, code: str) -> tuple[User, str]:
    """Check the code for a pending two-step sign-in; returns (user, next path)."""
    pending = pending_two_factor(request)
    if pending is None:
        end_session(request)
        raise NotAuthenticated(CODE_EXPIRED, code="two_factor_expired")
    limiter = request.app.state.login_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        raise RateLimited("Too many failed sign-in attempts. Please wait and try again.", limiter.retry_after(ip))
    user = db.get(User, pending["uid"])
    if user is None or not user.is_active or not user.totp_secret or user.session_version != pending["sv"]:
        end_session(request)
        raise NotAuthenticated(CODE_EXPIRED, code="two_factor_expired")
    if not account_service.check_login_code(db, user=user, code=code):
        limiter.record_failure(ip)
        raise NotAuthenticated(INVALID_CODE, code="invalid_code")
    return user, pending["next"]


# --- pages ---------------------------------------------------------------------


@router.get("/login", include_in_schema=False)
def login_page(
    request: Request,
    next: str | None = None,
    expired: int = 0,
    user: User | None = Depends(current_user_optional),
    db: Session = Depends(get_db),
):
    if user:
        return RedirectResponse(safe_next(next, user), status_code=303)
    notice = "Your session has ended. Please sign in again." if expired else None
    if request.query_params.get("signed_out"):
        notice = "You have been signed out."
    if request.query_params.get("password_changed"):
        notice = "Password changed. Please sign in with your new password."
    if request.query_params.get("closed"):
        notice = "Your account has been closed. Contact us if you would like it reopened."
    return render(request, "login.html", {"next": next or "", "notice": notice, "username": "", **public_context(db)})


@router.post("/login", include_in_schema=False)
def login_form(
    request: Request,
    username: str = Form(default="", max_length=64),
    password: str = Form(default="", max_length=1024),
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    context = {"next": next, "username": username.strip()[:64], "notice": None, **public_context(db)}
    if not csrf_token_valid(request, csrf_token):
        context["error"] = "Your sign-in form expired. Please try again."
        return render(request, "login.html", context, status_code=400)
    try:
        user = authenticate(request, db, username, password)
    except AppError as exc:
        context["error"] = exc.message
        return render(request, "login.html", context, status_code=exc.status_code)
    if user.totp_secret:
        begin_two_factor(request, user, safe_next(next, user))
        return RedirectResponse("/login/code", status_code=303)
    finish_sign_in(request, db, user)
    return RedirectResponse(safe_next(next, user), status_code=303)


@router.get("/login/code", include_in_schema=False)
def login_code_page(request: Request, db: Session = Depends(get_db)):
    if pending_two_factor(request) is None:
        return RedirectResponse("/login?expired=1", status_code=303)
    return render(request, "login_code.html", public_context(db))


@router.post("/login/code", include_in_schema=False)
def login_code_form(
    request: Request,
    code: str = Form(default="", max_length=20),
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
    db: Session = Depends(get_db),
):
    context = public_context(db)
    if not csrf_token_valid(request, csrf_token):
        context["error"] = "Your sign-in form expired. Please try again."
        return render(request, "login_code.html", context, status_code=400)
    try:
        user, next_path = verify_second_step(request, db, code)
    except AppError as exc:
        if exc.code == "two_factor_expired":
            return RedirectResponse("/login?expired=1", status_code=303)
        context["error"] = exc.message
        return render(request, "login_code.html", context, status_code=exc.status_code)
    finish_sign_in(request, db, user)
    return RedirectResponse(next_path, status_code=303)


@router.get("/forgot-password", include_in_schema=False)
def forgot_password_page(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    if user:
        return RedirectResponse(home_for(user), status_code=303)
    return render(request, "public/forgot_password.html", {"username": "", **public_context(db)})


@router.post("/forgot-password", include_in_schema=False)
def forgot_password_form(
    request: Request,
    username: str = Form(default="", max_length=64),
    recovery_code: str = Form(default="", max_length=64),
    new_password: str = Form(default="", max_length=1024),
    confirm_password: str = Form(default="", max_length=1024),
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
    db: Session = Depends(get_db),
):
    context = {"username": username.strip()[:64], **public_context(db)}

    def fail(message: str, status_code: int):
        context["error"] = message
        return render(request, "public/forgot_password.html", context, status_code=status_code)

    if not csrf_token_valid(request, csrf_token):
        return fail("Your form expired. Please try again.", 400)
    limiter = request.app.state.recovery_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        return fail("Too many attempts from your network. Please try again later.", 429)
    if new_password != confirm_password:
        return fail("The new passwords do not match.", 422)
    try:
        account_service.recover_password(db, username=username, code=recovery_code, new_password=new_password, ip=ip)
    except AppError as exc:
        if exc.code == "recovery_failed":
            limiter.record_failure(ip)
        return fail(exc.message, exc.status_code)
    end_session(request)
    return RedirectResponse("/login?password_changed=1", status_code=303)


@router.get("/logout", include_in_schema=False)
def logout_page(request: Request, user: User | None = Depends(current_user_optional)):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "logout.html", {"current_user": user})


@router.post("/logout", include_in_schema=False)
def logout_form(request: Request, csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD)):
    if not csrf_token_valid(request, csrf_token):
        # Do not sign out on a forged request; just send the user back.
        return RedirectResponse("/", status_code=303)
    end_session(request)
    return RedirectResponse("/login?signed_out=1", status_code=303)


# --- API -----------------------------------------------------------------------


@router.get("/api/csrf/", tags=["auth"])
def api_csrf(request: Request):
    """Return this session's CSRF token (required in ``X-CSRF-Token`` for POSTs)."""
    return {"csrf_token": get_csrf_token(request)}


@router.post("/api/login/", response_model=LoginResponse, tags=["auth"], dependencies=[Depends(verify_csrf)])
def api_login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = authenticate(request, db, payload.username, payload.password)
    if user.totp_secret:
        begin_two_factor(request, user, home_for(user))
        return JSONResponse({"two_factor_required": True, "csrf_token": get_csrf_token(request)}, status_code=202)
    token = finish_sign_in(request, db, user)
    return LoginResponse(user=UserSummary.from_user(user), redirect=home_for(user), csrf_token=token)


@router.post("/api/login/code/", response_model=LoginResponse, tags=["auth"], dependencies=[Depends(verify_csrf)])
def api_login_code(payload: LoginCodeRequest, request: Request, db: Session = Depends(get_db)):
    """Second step of sign-in for accounts with two-step sign-in turned on."""
    user, next_path = verify_second_step(request, db, payload.code)
    token = finish_sign_in(request, db, user)
    return LoginResponse(user=UserSummary.from_user(user), redirect=next_path, csrf_token=token)


@router.post("/api/logout/", response_model=OkResponse, tags=["auth"], dependencies=[Depends(verify_csrf)])
def api_logout(request: Request):
    end_session(request)
    return OkResponse()


@router.get("/api/me/", response_model=UserSummary, tags=["auth"])
def api_me(user: User = Depends(require_user)):
    return UserSummary.from_user(user)
