"""Application configuration.

Environment variables are loaded from the project-level ``.env`` file *before*
anything reads them. Set ``LEDGER_SKIP_DOTENV=1`` to bypass the file (the test
suite does this so it never touches real local credentials).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if os.getenv("LEDGER_SKIP_DOTENV") != "1":
    load_dotenv(PROJECT_ROOT / ".env")

PLACEHOLDER_PREFIX = "replace-with"
MIN_SECRET_KEY_LENGTH = 32
DEFAULT_DATABASE_URL = "sqlite:///./ledger.db"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
DEFAULT_SITE_NAME = "RefundClaimers"
DEFAULT_SUPPORT_EMAIL = "support@refundclaimers.com"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be 0 or 1 (got {raw!r}).")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got {raw!r}).") from exc


def resolve_database_url(url: str) -> str:
    """Anchor relative SQLite paths to the project root.

    ``sqlite:///./ledger.db`` would otherwise depend on the current working
    directory, which can silently create a second, empty database.
    """
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        path = url[len(prefix):]
        if path and path != ":memory:" and not Path(path).is_absolute():
            return prefix + str((PROJECT_ROOT / path).resolve())
    return url


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_url: str = DEFAULT_DATABASE_URL
    claim_upload_dir: Path = PROJECT_ROOT / "private_uploads"
    admin_username: str | None = None
    admin_password: str | None = None
    allow_negative: bool = False
    cookie_secure: bool = False
    session_max_age: int = SESSION_MAX_AGE_SECONDS
    seed_demo: bool = False
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300
    site_name: str = DEFAULT_SITE_NAME
    # The business address shown on the site and sent notifications. Every template
    # reads it through the ``support_email`` global; nothing hardcodes the address.
    support_email: str = DEFAULT_SUPPORT_EMAIL
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_secure: bool = True  # True = implicit TLS (SMTPS); False = STARTTLS
    smtp_user: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    mail_from: str | None = None

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            secret_key=os.getenv("SECRET_KEY", ""),
            database_url=resolve_database_url(os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL),
            claim_upload_dir=Path(os.getenv("CLAIM_UPLOAD_DIR", str(PROJECT_ROOT / "private_uploads"))).expanduser().resolve(),
            admin_username=os.getenv("ADMIN_USERNAME") or None,
            admin_password=os.getenv("ADMIN_PASSWORD") or None,
            allow_negative=_env_bool("ALLOW_NEGATIVE", False),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            session_max_age=_env_int("SESSION_MAX_AGE", SESSION_MAX_AGE_SECONDS),
            seed_demo=_env_bool("SEED_DEMO", False),
            login_rate_limit=_env_int("LOGIN_RATE_LIMIT", 10),
            login_rate_window_seconds=_env_int("LOGIN_RATE_WINDOW", 300),
            site_name=(os.getenv("SITE_NAME") or DEFAULT_SITE_NAME).strip()[:40] or DEFAULT_SITE_NAME,
            support_email=(os.getenv("SUPPORT_EMAIL") or DEFAULT_SUPPORT_EMAIL).strip(),
            smtp_host=(os.getenv("SMTP_HOST") or "").strip() or None,
            smtp_port=_env_int("SMTP_PORT", 465),
            smtp_secure=_env_bool("SMTP_SECURE", True),
            smtp_user=(os.getenv("SMTP_USER") or "").strip() or None,
            smtp_password=os.getenv("SMTP_PASSWORD") or None,
            mail_from=(os.getenv("MAIL_FROM") or "").strip() or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.secret_key or self.secret_key.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(
                "SECRET_KEY is not configured. Copy .env.example to .env and set SECRET_KEY to a long "
                "random value, e.g. the output of: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            raise ConfigError(f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters long.")
        if self.session_max_age <= 0:
            raise ConfigError("SESSION_MAX_AGE must be a positive number of seconds.")
        if self.login_rate_limit <= 0 or self.login_rate_window_seconds <= 0:
            raise ConfigError("LOGIN_RATE_LIMIT and LOGIN_RATE_WINDOW must be positive integers.")
        if not 1 <= self.smtp_port <= 65535:
            raise ConfigError("SMTP_PORT must be between 1 and 65535.")
