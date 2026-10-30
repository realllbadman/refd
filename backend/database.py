"""SQLAlchemy engine, session factory and request-scoped session dependency."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SQLITE_BUSY_TIMEOUT_SECONDS = 15


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    connect_args = {}
    if is_sqlite:
        # FastAPI runs sync dependencies in a threadpool, so a connection may be
        # used from a different thread than the one that created it.
        connect_args = {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}

    engine = create_engine(database_url, connect_args=connect_args, future=True)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in database_url:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Nullable columns added after the first release: (table, column, SQL type).
# Additive only; existing data is never rewritten.
ADDED_COLUMNS = (
    ("users", "display_name", "VARCHAR(60)"),
    ("users", "last_login_at", "DATETIME"),
    ("users", "signup_source", "VARCHAR(10)"),
    ("users", "totp_secret", "VARCHAR(64)"),
    ("users", "totp_last_step", "INTEGER"),
    ("balance_activity", "claim_id", "INTEGER REFERENCES refund_claims(id)"),
    ("balance_activity", "approved_by_id", "INTEGER REFERENCES users(id)"),
    ("refund_claims", "status_changed_at", "DATETIME"),
    ("refund_claims", "customer_seen_at", "DATETIME"),
    ("contact_messages", "reply_seen_at", "DATETIME"),
)

# Indexes on added columns; create_all() only builds indexes for brand-new tables.
ADDED_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_activity_claim ON balance_activity (claim_id) "
    "WHERE claim_id IS NOT NULL",
)

# One-time fill of refund_claims.status_changed_at for claims created before the
# column existed: the time of the last status event, else the submission time.
_BACKFILL_STATUS_CHANGED_AT = (
    "UPDATE refund_claims SET status_changed_at = COALESCE("
    "(SELECT MAX(e.created_at) FROM refund_claim_events e WHERE e.claim_id = refund_claims.id "
    "AND e.event_type IN ('submitted', 'status_change')), created_at) "
    "WHERE status_changed_at IS NULL"
)


def ensure_columns(engine: Engine) -> None:
    """Add missing nullable columns (and their indexes) to tables created by an older version."""
    with engine.begin() as conn:
        for table, column, sql_type in ADDED_COLUMNS:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        for statement in ADDED_INDEXES:
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql(_BACKFILL_STATUS_CHANGED_AT)


def init_db(engine: Engine) -> None:
    """Create missing tables and add missing columns. Never drops anything."""
    from backend import models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        ensure_columns(engine)


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.session_factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
