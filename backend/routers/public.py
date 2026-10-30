"""Public website: home, about, FAQ, terms, contact form and self-registration."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.database import get_db
from backend.dependencies import current_user_optional, get_settings
from backend.errors import AppError
from backend.models import User
from backend.routers.auth import home_for, public_context
from backend.security import CSRF_FORM_FIELD, client_ip, csrf_token_valid, start_session
from backend.services import account_service, mail_service, message_service
from backend.templating import render

router = APIRouter()


def _page(request: Request, template: str, db: Session, user: User | None, active: str, **extra):
    context = {"current_user": None, "visitor": user, "active_public": active, **public_context(db), **extra}
    return render(request, template, context)


@router.get("/", include_in_schema=False)
def home(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    return _page(request, "public/home.html", db, user, "home")


@router.get("/about", include_in_schema=False)
def about(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    return _page(request, "public/about.html", db, user, "about")


@router.get("/faq", include_in_schema=False)
def faq(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    return _page(request, "public/faq.html", db, user, "faq")


@router.get("/terms", include_in_schema=False)
def terms(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    return _page(request, "public/terms.html", db, user, "terms")


# --- contact ----------------------------------------------------------------------


@router.get("/contact", include_in_schema=False)
def contact_page(request: Request, sent: int = 0, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    form = {"name": user.label if user else "", "email": "", "subject": "", "message": ""}
    return _page(request, "public/contact.html", db, user, "contact", form=form, sent=bool(sent), errors={})


@router.post("/contact", include_in_schema=False)
def contact_submit(
    request: Request,
    tasks: BackgroundTasks,
    name: str = Form(default="", max_length=500),
    email: str = Form(default="", max_length=500),
    subject: str = Form(default="", max_length=500),
    message: str = Form(default="", max_length=10000),
    website: str = Form(default="", max_length=500),  # honeypot: humans never see or fill it
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
    user: User | None = Depends(current_user_optional),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    form = {"name": name, "email": email, "subject": subject, "message": message}
    context = {"form": form, "sent": False, "errors": {}}
    if not csrf_token_valid(request, csrf_token):
        context["error"] = "Your form expired. Please submit it again."
        return _status_page(request, db, user, "public/contact.html", "contact", 400, context)
    if website:
        return RedirectResponse("/contact?sent=1", status_code=303)
    limiter = request.app.state.contact_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        context["error"] = "You have sent several messages recently. Please try again later."
        return _status_page(request, db, user, "public/contact.html", "contact", 429, context)
    try:
        saved = message_service.create(db, name=name, email=email, subject=subject, body=message, user=user)
    except AppError as exc:
        context["error"] = exc.message
        context["errors"] = {d["field"]: d["message"] for d in (exc.details or []) if d.get("field")}
        return _status_page(request, db, user, "public/contact.html", "contact", exc.status_code, context)
    limiter.record_failure(ip)  # counts submissions, not failures
    mail_service.queue_request_emails(
        tasks, settings,
        kind="Contact form",
        subject=saved.subject,
        fields=[("Message ID", f"#{saved.id}"), ("Name", saved.name), ("Email", saved.email or "Not given"),
                ("Account", f"@{user.username} (#{user.id})" if user else "Visitor"),
                ("Subject", saved.subject), ("Message", saved.body)],
        submitter_email=saved.email,
        ack_subject="We received your message",
        ack_line="We've received your message and the team will reply as soon as they can.",
    )
    return RedirectResponse("/contact?sent=1", status_code=303)


def _status_page(request, db, user, template, active, status_code, context):
    full = {"current_user": None, "visitor": user, "active_public": active, **public_context(db), **context}
    return render(request, template, full, status_code=status_code)


# --- registration -----------------------------------------------------------------


@router.get("/register", include_in_schema=False)
def register_page(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    if user:
        return RedirectResponse(home_for(user), status_code=303)
    return _page(request, "public/register.html", db, None, "register", form={"username": "", "display_name": ""}, errors={})


@router.post("/register", include_in_schema=False)
def register_submit(
    request: Request,
    username: str = Form(default="", max_length=64),
    display_name: str = Form(default="", max_length=200),
    password: str = Form(default="", max_length=1024),
    confirm_password: str = Form(default="", max_length=1024),
    accept_terms: str = Form(default=""),
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
    db: Session = Depends(get_db),
):
    context = {"form": {"username": username.strip()[:64], "display_name": display_name.strip()[:60]}, "errors": {}}

    def fail(message: str, status_code: int, errors: dict | None = None):
        context["error"] = message
        context["errors"] = errors or {}
        return _status_page(request, db, None, "public/register.html", "register", status_code, context)

    if not csrf_token_valid(request, csrf_token):
        return fail("Your form expired. Please try again.", 400)
    if not public_context(db)["registration_open"]:
        return fail("New sign-ups are currently closed by the administrator.", 403)
    limiter = request.app.state.register_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        return fail("Too many sign-up attempts from your network. Please try again later.", 429)
    if password != confirm_password:
        return fail("The passwords do not match.", 422, {"confirm_password": "Passwords do not match."})
    if not accept_terms:
        return fail("Please confirm you have read the terms.", 422, {"accept_terms": "Required."})
    limiter.record_failure(ip)  # counts every attempt that reaches account creation
    try:
        user = account_service.register_account(
            db, username=username, password=password, display_name=display_name, ip=ip
        )
    except AppError as exc:
        errors = {d["field"]: d["message"] for d in (exc.details or []) if d.get("field")}
        return fail(exc.message, exc.status_code, errors)
    start_session(request, user)
    account_service.record_login(db, user, ip)
    return RedirectResponse("/dashboard?welcome=1", status_code=303)
