"""Jinja2 environment and a render helper that injects shared page context."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from backend.money import format_cents
from backend.security import CHAT_CONTENT_SECURITY_POLICY, get_csrf_token

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
# Password-entry pages never load third-party chat scripts.
CHAT_HIDDEN_TEMPLATES = {"login.html", "login_code.html", "public/register.html", "public/forgot_password.html", "user/profile.html"}


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_display(value: datetime | None) -> str:
    return value.strftime("%b %d, %Y %H:%M UTC") if value else ""


def build_templates() -> Jinja2Templates:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        auto_reload=True,
    )
    env.filters["money"] = format_cents
    env.filters["iso_utc"] = _iso_utc
    env.filters["utc_display"] = _utc_display
    # Make the icon macro available in every template without an explicit import.
    env.globals["icon"] = env.get_template("_icons.html").module.icon
    return Jinja2Templates(env=env)


def _show_chat(request: Request, name: str, ctx: dict) -> bool:
    # Public pages pass current_user=None and carry a signed-in user as "visitor".
    signed_in = ctx["current_user"] or ctx.get("visitor") or getattr(request.state, "current_user", None)
    # Administrators see every account's data, so third-party scripts never load for them.
    if signed_in and signed_in.is_admin:
        return False
    return name not in CHAT_HIDDEN_TEMPLATES


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"csrf_token": get_csrf_token(request), "current_user": None}
    ctx.update(context or {})
    ctx["show_chat"] = _show_chat(request, name, ctx)
    response = request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if ctx["show_chat"]:
        response.headers["Content-Security-Policy"] = CHAT_CONTENT_SECURITY_POLICY
    return response
