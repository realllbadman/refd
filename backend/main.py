"""Application entry point: ``uvicorn backend.main:app --reload --port 8015``."""

from __future__ import annotations

# Configuration must be imported first: it loads .env before anything reads it.
from backend.config import Settings  # isort: skip

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend.bootstrap import ensure_admin, seed_demo_accounts
from backend.database import build_engine, build_session_factory, init_db
from backend.errors import register_exception_handlers
from backend.routers import admin, auth, public, user
from backend.security import CONTENT_SECURITY_POLICY, LoginRateLimiter
from backend.templating import build_templates

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "ledger_session"


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ledger")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        with session_factory() as db:
            ensure_admin(db, settings)
            if settings.seed_demo:
                seed_demo_accounts(db)
        logger.info("%s ready (database: %s)", settings.site_name, engine.url.render_as_string(hide_password=True))
        if not settings.mail_configured:
            logger.warning("Outgoing email is off: set SMTP_HOST, SMTP_USER and SMTP_PASSWORD to turn it on.")
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.site_name,
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = build_templates()
    app.state.templates.env.globals["site_name"] = settings.site_name
    app.state.templates.env.globals["support_email"] = settings.support_email
    app.state.templates.env.globals["mail_configured"] = settings.mail_configured
    app.state.login_limiter = LoginRateLimiter(settings.login_rate_limit, settings.login_rate_window_seconds)
    # Abuse limits for the public forms (process-local, like the login limiter).
    app.state.contact_limiter = LoginRateLimiter(max_failures=5, window_seconds=600)
    app.state.register_limiter = LoginRateLimiter(max_failures=10, window_seconds=3600)
    app.state.claim_limiter = LoginRateLimiter(max_failures=5, window_seconds=3600)
    app.state.claim_evidence_limiter = LoginRateLimiter(max_failures=20, window_seconds=3600)
    app.state.recovery_limiter = LoginRateLimiter(max_failures=5, window_seconds=3600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Added last so it wraps everything above (sessions available everywhere).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    register_exception_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(user.router)
    app.include_router(admin.router)
    return app


app = create_app()
