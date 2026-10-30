"""Application exceptions and their JSON / HTML handlers.

API errors always have the shape::

    {"error": {"code": "not_found", "message": "User not found.", "details": [...]}}
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("ledger")


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class NotAuthenticated(AppError):
    status_code = 401
    code = "not_authenticated"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class LoginRequired(Exception):
    """Raised by page dependencies; redirects browsers to the login page."""

    def __init__(self, expired: bool = False):
        self.expired = expired


class PageForbidden(Exception):
    """Raised by page dependencies; renders the 403 page."""


def is_api_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def error_json(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status, headers=headers)


def render_error_page(request: Request, status: int) -> Response:
    from backend.templating import render

    template = {403: "403.html", 404: "404.html"}.get(status, "500.html")
    user = getattr(request.state, "current_user", None)
    if user is None and "session" in request.scope:
        # Routes without auth dependencies (e.g. unknown URLs) still show the signed-in header.
        from backend.dependencies import get_current_user

        try:
            with request.app.state.session_factory() as db:
                get_current_user(request, db)
            user = getattr(request.state, "current_user", None)
        except Exception:  # never let the error page itself fail
            user = None
    return render(request, template, {"status_code": status, "current_user": user}, status_code=status)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        if is_api_request(request):
            headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
            return error_json(exc.status_code, exc.code, exc.message, exc.details, headers)
        if exc.status_code in (403, 404):
            return render_error_page(request, exc.status_code)
        return render_error_page(request, 500 if exc.status_code >= 500 else exc.status_code)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        location = f"/login?next={quote(target, safe='')}"
        if exc.expired:
            location += "&expired=1"
        return RedirectResponse(location, status_code=303)

    @app.exception_handler(PageForbidden)
    async def _page_forbidden(request: Request, exc: PageForbidden):
        return render_error_page(request, 403)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Never echo submitted values back (they may contain passwords).
        details = []
        for err in exc.errors():
            loc = [
                str(part)
                for part in err.get("loc", ())
                # drop location prefixes and discriminated-union tags ("set"/"adjust")
                if part not in ("body", "query", "path", "header", "set", "adjust")
                and not str(part).endswith("Request")
            ]
            msg = str(err.get("msg", "Invalid value"))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            details.append({"field": ".".join(loc) or None, "message": msg})
        message = details[0]["message"] if details else "Invalid request."
        if details and details[0]["field"]:
            message = f"{details[0]['field']}: {message}"
        if is_api_request(request):
            return error_json(422, "validation_error", message, details)
        return render_error_page(request, 404)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if is_api_request(request):
            codes = {401: "not_authenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
            message = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, "Request failed.")
            return error_json(exc.status_code, codes.get(exc.status_code, "error"), message)
        if exc.status_code in (403, 404):
            return render_error_page(request, exc.status_code)
        if exc.status_code == 405:
            return render_error_page(request, 404)
        return render_error_page(request, 500 if exc.status_code >= 500 else exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        if is_api_request(request):
            return error_json(500, "server_error", "Something went wrong. Please try again.")
        return render_error_page(request, 500)
