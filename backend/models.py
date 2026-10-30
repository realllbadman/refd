"""ORM models. Monetary values are always integer cents."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

ROLE_USER = "user"
ROLE_ADMIN = "admin"
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
CHANGE_SET = "set"
CHANGE_ADJUST = "adjust"


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone-aware column type)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default=ROLE_USER)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=STATUS_ACTIVE)
    balance_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Bumped whenever existing sessions must stop working (e.g. deactivation).
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    # Added after the first release; database.ensure_columns() adds them to older databases.
    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signup_source: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "admin" | "self"
    # Two-step sign-in (administrators): base32 TOTP secret and the last accepted time step.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)

    activity: Mapped[list["BalanceActivity"]] = relationship(
        back_populates="user",
        foreign_keys="BalanceActivity.user_id",
        order_by="BalanceActivity.id.desc()",
        passive_deletes="all",
    )

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


class BalanceActivity(Base):
    """Append-only record of an application-recorded balance change."""

    __tablename__ = "balance_activity"
    __table_args__ = (
        CheckConstraint("change_type IN ('set', 'adjust')", name="ck_activity_change_type"),
        CheckConstraint("resulting_cents = previous_cents + amount_cents", name="ck_activity_arithmetic"),
        UniqueConstraint("admin_id", "idempotency_key", name="uq_activity_admin_idempotency"),
        Index("ix_balance_activity_user_created", "user_id", "created_at"),
        # A refund claim can be credited at most once.
        Index("uq_balance_activity_claim", "claim_id", unique=True, sqlite_where=text("claim_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)
    previous_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    # Added after the first release (database.ensure_columns()).
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("refund_claims.id", ondelete="RESTRICT"), nullable=True)
    # Second administrator who approved a change above the approval threshold.
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    user: Mapped[User] = relationship(back_populates="activity", foreign_keys=[user_id])
    admin: Mapped[User] = relationship(foreign_keys=[admin_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    claim: Mapped[RefundClaim | None] = relationship(foreign_keys=[claim_id])


MESSAGE_NEW = "new"
MESSAGE_READ = "read"
MESSAGE_ARCHIVED = "archived"


class ContactMessage(Base):
    """A contact-form submission or a signed-in user's support message."""

    __tablename__ = "contact_messages"
    __table_args__ = (
        CheckConstraint("status IN ('new', 'read', 'archived')", name="ck_messages_status"),
        Index("ix_contact_messages_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=MESSAGE_NEW)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replied_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    # When the account holder last saw the reply (drives the unread badge).
    reply_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    replied_by: Mapped[User | None] = relationship(foreign_keys=[replied_by_id])


CLAIM_SUBMITTED = "submitted"
CLAIM_REVIEWING = "under_review"
CLAIM_AWAITING_CUSTOMER = "awaiting_customer"
CLAIM_APPROVED = "approved"
CLAIM_DECLINED = "declined"
CLAIM_CANCELLED = "cancelled"
CLAIM_STATUSES = (
    CLAIM_SUBMITTED,
    CLAIM_REVIEWING,
    CLAIM_AWAITING_CUSTOMER,
    CLAIM_APPROVED,
    CLAIM_DECLINED,
    CLAIM_CANCELLED,
)


class RefundClaim(Base):
    """A refund request and its current review state."""

    __tablename__ = "refund_claims"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted', 'under_review', 'awaiting_customer', 'approved', 'declined', 'cancelled')",
            name="ck_refund_claims_status",
        ),
        CheckConstraint("priority IN ('normal', 'high', 'urgent')", name="ck_refund_claims_priority"),
        CheckConstraint("amount_cents > 0", name="ck_refund_claims_amount_positive"),
        Index("ix_refund_claims_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    merchant: Mapped[str] = mapped_column(String(120), nullable=False)
    order_reference: Mapped[str] = mapped_column(String(80), nullable=False)
    purchase_date: Mapped[Date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=CLAIM_SUBMITTED)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")
    assigned_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    # Added after the first release (database.ensure_columns()).
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    customer_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    assigned_admin: Mapped[User | None] = relationship(foreign_keys=[assigned_admin_id])
    events: Mapped[list["RefundClaimEvent"]] = relationship(
        back_populates="claim", order_by="(RefundClaimEvent.created_at, RefundClaimEvent.id)"
    )
    evidence: Mapped[list["RefundClaimEvidence"]] = relationship(
        back_populates="claim", order_by="(RefundClaimEvidence.created_at, RefundClaimEvidence.id)"
    )


class RefundClaimEvent(Base):
    """Append-only public update or internal review note for a refund claim."""

    __tablename__ = "refund_claim_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('submitted', 'status_change', 'customer_message', 'admin_reply', 'admin_note', 'evidence_added')",
            name="ck_refund_claim_events_type",
        ),
        Index("ix_refund_claim_events_claim_created", "claim_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("refund_claims.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    claim: Mapped[RefundClaim] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship(foreign_keys=[actor_id])


class RefundClaimEvidence(Base):
    """Private uploaded document associated with a refund claim."""

    __tablename__ = "refund_claim_evidence"
    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 8388608", name="ck_refund_evidence_size"),
        Index("ix_refund_claim_evidence_claim_created", "claim_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("refund_claims.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    claim: Mapped[RefundClaim] = relationship(back_populates="evidence")
    uploaded_by: Mapped[User] = relationship()


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")  # info | success | warning
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    author: Mapped[User | None] = relationship()


class AccountNote(Base):
    """Internal administrator note about an account (never shown to the account holder)."""

    __tablename__ = "account_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    author: Mapped[User | None] = relationship(foreign_keys=[author_id])


class AuditEntry(Base):
    """Append-only record of administrative and account events."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_id])
    target: Mapped[User | None] = relationship(foreign_keys=[target_user_id])


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class SavedReply(Base):
    """Reusable reply text for claim updates and support messages."""

    __tablename__ = "saved_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_CANCELLED = "cancelled"


class PendingBalanceChange(Base):
    """A balance change at or above the approval threshold, waiting for a second administrator."""

    __tablename__ = "pending_balance_changes"
    __table_args__ = (
        CheckConstraint("mode IN ('set', 'adjust')", name="ck_pending_mode"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')", name="ck_pending_status"),
        UniqueConstraint("requested_by_id", "idempotency_key", name="uq_pending_requester_idempotency"),
        Index("ix_pending_balance_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    value_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # target (set) or signed delta (adjust)
    balance_at_request_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("refund_claims.id", ondelete="RESTRICT"), nullable=True)
    claim_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=APPROVAL_PENDING)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    activity_id: Mapped[int | None] = mapped_column(ForeignKey("balance_activity.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    requested_by: Mapped[User] = relationship(foreign_keys=[requested_by_id])
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id])
    claim: Mapped[RefundClaim | None] = relationship(foreign_keys=[claim_id])


class RecoveryCode(Base):
    """One-time code an account holder can use to reset a forgotten password.

    Only a SHA-256 digest is stored; codes carry 80 random bits, so a fast hash is enough.
    """

    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


# Defence in depth: the application never updates or deletes activity or audit
# rows, and the database refuses to as well.
for _table in (BalanceActivity.__table__, AuditEntry.__table__, RefundClaimEvent.__table__):
    for _op in ("UPDATE", "DELETE"):
        event.listen(
            _table,
            "after_create",
            DDL(
                f"CREATE TRIGGER IF NOT EXISTS trg_{_table.name}_no_{_op.lower()} "
                f"BEFORE {_op} ON {_table.name} "
                f"BEGIN SELECT RAISE(ABORT, '{_table.name} is append-only'); END;"
            ).execute_if(dialect="sqlite"),
        )
