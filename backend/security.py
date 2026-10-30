"""Password hashing, session helpers, CSRF tokens and login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from collections import deque
from urllib.parse import quote

from fastapi import Request
from passlib.context import CryptContext

from backend.models import User

BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 8

SESSION_USER_KEY = "uid"
SESSION_VERSION_KEY = "sv"
SESSION_CSRF_KEY = "csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

# Sent instead of the policy above only on pages that load the Smartsupp live
# chat (static/js/smartsupp.js): every page except password-entry pages and
# anything an administrator opens. Its injected styles need 'unsafe-inline'.
_SMARTSUPP = "https://*.smartsupp.com https://*.smartsuppchat.com https://*.smartsuppcdn.com"
_SMARTSUPP_WS = "wss://*.smartsupp.com wss://*.smartsuppchat.com"
CHAT_CONTENT_SECURITY_POLICY = (
    f"default-src 'self'; script-src 'self' {_SMARTSUPP}; "
    f"style-src 'self' 'unsafe-inline' {_SMARTSUPP}; img-src 'self' data: blob: {_SMARTSUPP}; "
    f"font-src 'self' {_SMARTSUPP}; connect-src 'self' {_SMARTSUPP} {_SMARTSUPP_WS}; "
    f"frame-src {_SMARTSUPP}; media-src 'self' {_SMARTSUPP}; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)
_DUMMY_HASH = _pwd_context.hash("timing-equaliser-not-a-real-password")


class PasswordPolicyError(ValueError):
    pass


def password_too_long(password: str) -> bool:
    return len(password.encode("utf-8")) > BCRYPT_MAX_BYTES


def validate_new_password(password: str) -> None:
    """Policy for passwords being set. bcrypt only reads the first 72 bytes, so
    longer passwords are rejected rather than silently truncated."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password_too_long(password):
        raise PasswordPolicyError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes (UTF-8 encoded).")
    if password.strip() != password:
        raise PasswordPolicyError("Password must not start or end with whitespace.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        raise PasswordPolicyError("Password must not contain control characters.")


def hash_password(password: str) -> str:
    if password_too_long(password):
        raise PasswordPolicyError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes (UTF-8 encoded).")
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time verification; also burns a hash when the user is unknown."""
    if password_too_long(password):
        return False
    if not password_hash:
        _pwd_context.verify(password, _DUMMY_HASH)
        return False
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        return False


def generate_temporary_password(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in candidate) and any(c.isupper() for c in candidate) and any(c.islower() for c in candidate):
            return candidate


# --- sessions --------------------------------------------------------------


def start_session(request: Request, user: User) -> str:
    """Replace any existing session with a fresh authenticated one."""
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_VERSION_KEY] = user.session_version
    return rotate_csrf_token(request)


def end_session(request: Request) -> None:
    request.session.clear()


def get_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = rotate_csrf_token(request)
    return token


def rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[SESSION_CSRF_KEY] = token
    return token


def csrf_token_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected.encode(), submitted.encode())


# --- two-step sign-in (TOTP, RFC 6238) -------------------------------------------

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept one step either side for clock drift
TWO_FACTOR_SESSION_KEY = "2fa"
TWO_FACTOR_TTL_SECONDS = 300
TOTP_SETUP_SESSION_KEY = "totp_setup"


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return str(code).zfill(TOTP_DIGITS)


def totp_code(secret: str, at: float | None = None) -> str:
    return _totp_at(secret, int((time.time() if at is None else at) // TOTP_PERIOD))


def verify_totp(secret: str, code: str, *, last_step: int | None = None, at: float | None = None) -> int | None:
    """Return the matching time step, or None. Steps at or before ``last_step`` are refused (no replay)."""
    code = "".join((code or "").split())
    if not secret or len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    now_step = int((time.time() if at is None else at) // TOTP_PERIOD)
    for step in range(now_step - TOTP_WINDOW, now_step + TOTP_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"


def begin_two_factor(request: Request, user: User, next_path: str) -> None:
    """Password accepted; remember who is signing in until the code step (not signed in yet)."""
    request.session.clear()
    request.session[TWO_FACTOR_SESSION_KEY] = {
        "uid": user.id, "sv": user.session_version, "next": next_path, "at": int(time.time()),
    }
    rotate_csrf_token(request)


def pending_two_factor(request: Request) -> dict | None:
    data = request.session.get(TWO_FACTOR_SESSION_KEY)
    if not isinstance(data, dict) or time.time() - data.get("at", 0) > TWO_FACTOR_TTL_SECONDS:
        return None
    return data


# --- recovery codes -------------------------------------------------------------

RECOVERY_CODE_COUNT = 8
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def generate_recovery_code() -> str:
    """16 characters from a 31-symbol alphabet (~79 bits), shown as XXXX-XXXX-XXXX-XXXX."""
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def hash_recovery_code(code: str) -> str:
    normalized = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()


# --- rate limiting ------------------------------------------------------------


class LoginRateLimiter:
    """Per-IP sliding window counting *failed* logins.

    Process-local and in-memory: it resets on restart and is not shared between
    worker processes. Adequate for a single-process local deployment only.
    """

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._failures.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        return bucket

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())

    def retry_after(self, key: str) -> int:
        with self._lock:
            bucket = self._prune(key, time.monotonic())
            if not bucket:
                return 0
            return max(1, int(self.window_seconds - (time.monotonic() - bucket[0])) + 1)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
