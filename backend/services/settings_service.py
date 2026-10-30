"""Runtime settings an administrator can change from the panel."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.errors import Conflict, ValidationFailed
from backend.models import AppSetting, User, utcnow
from backend.money import MAX_AMOUNT_CENTS, format_cents
from backend.services import audit_service

DEFAULTS = {
    "registration_open": "1",
    "public_notice": "",
    # Balance changes of at least this many cents need a second administrator (0 = off).
    "approval_threshold_cents": "0",
    # "Awaiting customer" claims older than this many days can be closed as stale.
    "stale_claim_days": "14",
}
PUBLIC_NOTICE_MAX = 200
STALE_DAYS_RANGE = (1, 365)


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_all(db: Session) -> dict:
    stored = {row.key: row.value for row in db.scalars(select(AppSetting)).all()}
    raw = {**DEFAULTS, **stored}
    return {
        "registration_open": raw["registration_open"] == "1",
        "public_notice": raw["public_notice"],
        "approval_threshold_cents": max(0, _int(raw["approval_threshold_cents"], 0)),
        "stale_claim_days": _int(raw["stale_claim_days"], 14),
    }


def registration_open(db: Session) -> bool:
    return get_all(db)["registration_open"]


def approval_threshold_cents(db: Session) -> int:
    return get_all(db)["approval_threshold_cents"]


def stale_claim_days(db: Session) -> int:
    return get_all(db)["stale_claim_days"]


def update(
    db: Session,
    *,
    actor: User,
    registration_open: bool | None = None,
    public_notice: str | None = None,
    approval_threshold_cents: int | None = None,
    stale_claim_days: int | None = None,
) -> dict:
    from backend.services.account_service import count_active_admins

    changes = {}
    summary = []
    if registration_open is not None:
        changes["registration_open"] = "1" if registration_open else "0"
        summary.append(f"registration {'open' if registration_open else 'closed'}")
    if public_notice is not None:
        changes["public_notice"] = " ".join(public_notice.split())[:PUBLIC_NOTICE_MAX]
        summary.append("public notice updated")
    if approval_threshold_cents is not None:
        if not 0 <= approval_threshold_cents <= MAX_AMOUNT_CENTS:
            raise ValidationFailed("Approval threshold is out of range.",
                                   details=[{"field": "approval_threshold", "message": "Out of range."}])
        if approval_threshold_cents and count_active_admins(db) < 2:
            raise Conflict("Second-admin approval needs at least two active administrators.",
                           code="needs_second_admin",
                           details=[{"field": "approval_threshold", "message": "Add another administrator first."}])
        changes["approval_threshold_cents"] = str(approval_threshold_cents)
        summary.append(f"approval threshold {format_cents(approval_threshold_cents) if approval_threshold_cents else 'off'}")
    if stale_claim_days is not None:
        low, high = STALE_DAYS_RANGE
        if not low <= stale_claim_days <= high:
            raise ValidationFailed(f"Stale claim days must be between {low} and {high}.",
                                   details=[{"field": "stale_claim_days", "message": "Out of range."}])
        changes["stale_claim_days"] = str(stale_claim_days)
        summary.append(f"stale claims after {stale_claim_days} days")
    current = {row.key: row for row in db.scalars(select(AppSetting)).all()}
    for key, value in changes.items():
        row = current.get(key)
        if row is None:
            db.add(AppSetting(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()
    if changes:
        audit_service.record(db, action="settings.updated", actor_id=actor.id, details=", ".join(summary))
    db.commit()
    return get_all(db)
