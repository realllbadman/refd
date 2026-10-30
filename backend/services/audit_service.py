"""Append-only audit log of administrative and account events."""

from __future__ import annotations

import math

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from backend.models import AuditEntry, User, utcnow

ACTION_LABELS = {
    "auth.login": "Signed in",
    "account.registered": "Registered (self sign-up)",
    "account.created": "Account created by admin",
    "account.activated": "Account reactivated",
    "account.deactivated": "Account deactivated",
    "account.password_reset": "Password reset by admin",
    "account.password_changed": "Password changed",
    "account.profile_updated": "Profile updated",
    "account.note_added": "Note added",
    "account.note_deleted": "Note deleted",
    "account.closed": "Account closed by holder",
    "account.recovery_codes_created": "Recovery codes created",
    "account.password_recovered": "Password reset with recovery code",
    "account.totp_enabled": "Two-step sign-in turned on",
    "account.totp_disabled": "Two-step sign-in turned off",
    "account.totp_reset": "Two-step sign-in reset by admin",
    "balance.set": "Balance set",
    "balance.adjust": "Balance adjusted",
    "balance.approval_requested": "Balance change sent for approval",
    "balance.approval_rejected": "Balance change rejected",
    "balance.approval_cancelled": "Balance change request cancelled",
    "message.replied": "Message replied to",
    "claim.created": "Refund claim submitted",
    "claim.updated": "Refund claim reviewed",
    "claim.evidence_added": "Claim evidence uploaded",
    "claim.edited": "Refund claim edited by holder",
    "claim.closed_stale": "Stale claims closed",
    "saved_reply.created": "Saved reply created",
    "saved_reply.updated": "Saved reply updated",
    "saved_reply.deleted": "Saved reply deleted",
    "announcement.created": "Announcement created",
    "announcement.updated": "Announcement updated",
    "announcement.deleted": "Announcement deleted",
    "settings.updated": "Settings updated",
}
MAX_PAGE_SIZE = 100


def record(
    db: Session,
    *,
    action: str,
    actor_id: int | None = None,
    target_user_id: int | None = None,
    details: str | None = None,
    ip: str | None = None,
) -> AuditEntry:
    """Add an entry to the current transaction; the caller commits."""
    entry = AuditEntry(
        action=action,
        actor_id=actor_id,
        target_user_id=target_user_id,
        details=(details or None) and details[:300],
        ip=ip,
        created_at=utcnow(),
    )
    db.add(entry)
    return entry


def list_entries(db: Session, *, action: str = "", q: str = "", page: int = 1, page_size: int = 25):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    conditions = []
    if action:
        conditions.append(AuditEntry.action.like(f"{action.split('.')[0]}.%") if "." not in action else AuditEntry.action == action)
    if q.strip():
        term = q.strip().lower()
        matching = select(User.id).where(User.username.like(f"%{term}%"))
        conditions.append(or_(AuditEntry.actor_id.in_(matching), AuditEntry.target_user_id.in_(matching)))
    total = db.scalar(select(func.count()).select_from(AuditEntry).where(*conditions))
    rows = db.scalars(
        select(AuditEntry)
        .where(*conditions)
        .options(selectinload(AuditEntry.actor), selectinload(AuditEntry.target))
        .order_by(AuditEntry.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return rows, total, page, page_size, max(1, math.ceil(total / page_size))
