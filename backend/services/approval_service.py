"""Second-administrator approval for large balance changes.

When the approval threshold (an admin setting, 0 = off) is set, any change whose
size is at least the threshold is stored as a ``PendingBalanceChange`` instead of
being applied. A *different* active administrator then approves it, which runs
the normal balance engine (recorded by the requester, approved by the approver),
or rejects it. The requester can cancel their own request.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from backend.errors import AppError, Conflict, Forbidden, NotFound, ValidationFailed
from backend.models import (
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    CHANGE_ADJUST,
    CHANGE_SET,
    BalanceActivity,
    PendingBalanceChange,
    User,
    utcnow,
)
from backend.money import format_cents
from backend.services import audit_service, balance_service, settings_service

RECENT_DECIDED_LIMIT = 20


@dataclass
class ChangeOutcome:
    """Either an applied change (``result``) or a request waiting for approval (``pending``)."""

    result: balance_service.BalanceResult | None = None
    pending: PendingBalanceChange | None = None


def needs_approval(db: Session, *, mode: str, value: int, previous: int) -> bool:
    threshold = settings_service.approval_threshold_cents(db)
    if not threshold:
        return False
    delta = value - previous if mode == CHANGE_SET else value
    return abs(delta) >= threshold


def _describe(pending: PendingBalanceChange) -> str:
    if pending.mode == CHANGE_SET:
        return f"set to {format_cents(pending.value_cents)}"
    return f"adjust {format_cents(pending.value_cents, signed=True)}"


def request_change(
    db: Session,
    *,
    admin: User,
    user_id: int,
    mode: str,
    value: int,
    reason: str | None,
    allow_negative: bool,
    idempotency_key: str | None = None,
    claim_id: int | None = None,
    claim_reply: str | None = None,
    commit: bool = True,
) -> PendingBalanceChange:
    key = balance_service.validate_idempotency_key(idempotency_key)
    if key:
        existing = db.scalar(select(PendingBalanceChange).where(
            PendingBalanceChange.requested_by_id == admin.id, PendingBalanceChange.idempotency_key == key))
        if existing is not None:
            if (existing.user_id, existing.mode, existing.value_cents, existing.reason) != (user_id, mode, value, reason):
                raise Conflict("This Idempotency-Key was already used for a different balance operation.",
                               code="idempotency_mismatch")
            return existing
    # Reject obviously invalid requests now rather than at approval time.
    previous, _ = balance_service.preview(db, user_id=user_id, mode=mode, value=value, allow_negative=allow_negative)
    if claim_id is not None and db.scalar(select(PendingBalanceChange.id).where(
            PendingBalanceChange.claim_id == claim_id, PendingBalanceChange.status == APPROVAL_PENDING)) is not None:
        raise Conflict("A credit for this claim is already waiting for approval.", code="claim_credit_pending")
    pending = PendingBalanceChange(
        user_id=user_id,
        requested_by_id=admin.id,
        mode=mode,
        value_cents=value,
        balance_at_request_cents=previous,
        reason=reason,
        idempotency_key=key,
        claim_id=claim_id,
        claim_reply=claim_reply,
        status=APPROVAL_PENDING,
        created_at=utcnow(),
    )
    db.add(pending)
    db.flush()
    audit_service.record(
        db, action="balance.approval_requested", actor_id=admin.id, target_user_id=user_id,
        details=f"Request #{pending.id}: {_describe(pending)}" + (f" · {reason}" if reason else ""),
    )
    if commit:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise Conflict("This request was already submitted.", code="duplicate_request")
        db.refresh(pending)
    return pending


def submit_change(
    db: Session,
    *,
    admin: User,
    user_id: int,
    mode: str,
    value: int,
    reason: str | None,
    allow_negative: bool,
    idempotency_key: str | None = None,
) -> ChangeOutcome:
    """Apply a change directly, or queue it when it reaches the approval threshold."""
    if mode not in (CHANGE_SET, CHANGE_ADJUST):
        raise ValidationFailed("Mode must be 'set' or 'adjust'.")
    key = balance_service.validate_idempotency_key(idempotency_key)
    # A retry of a change that was applied directly must replay, not be re-queued.
    if key and db.scalar(select(BalanceActivity.id).where(
            BalanceActivity.admin_id == admin.id, BalanceActivity.idempotency_key == key)) is not None:
        return ChangeOutcome(result=balance_service.apply_change(
            db, admin=admin, user_id=user_id, mode=mode, value=value, reason=reason,
            allow_negative=allow_negative, idempotency_key=key))
    previous, _ = balance_service.preview(db, user_id=user_id, mode=mode, value=value, allow_negative=allow_negative)
    queued = key and db.scalar(select(PendingBalanceChange.id).where(
        PendingBalanceChange.requested_by_id == admin.id, PendingBalanceChange.idempotency_key == key)) is not None
    if queued or needs_approval(db, mode=mode, value=value, previous=previous):
        return ChangeOutcome(pending=request_change(
            db, admin=admin, user_id=user_id, mode=mode, value=value, reason=reason,
            allow_negative=allow_negative, idempotency_key=key))
    return ChangeOutcome(result=balance_service.apply_change(
        db, admin=admin, user_id=user_id, mode=mode, value=value, reason=reason,
        allow_negative=allow_negative, idempotency_key=key))


def bulk_adjust(db: Session, *, admin: User, user_ids: list[int], amount_cents: int, reason: str | None,
                allow_negative: bool) -> list[dict]:
    """Like ``balance_service.bulk_adjust``, but changes at the threshold are queued per account."""
    results = []
    for user_id in user_ids:
        try:
            outcome = submit_change(db, admin=admin, user_id=user_id, mode=CHANGE_ADJUST, value=amount_cents,
                                    reason=reason, allow_negative=allow_negative)
            if outcome.pending is not None:
                user = db.get(User, user_id)
                results.append({"user_id": user_id, "ok": True, "pending": True, "username": user.username,
                                "request_id": outcome.pending.id})
            else:
                result = outcome.result
                results.append({
                    "user_id": user_id, "ok": True, "username": result.user.username,
                    "resulting_cents": result.activity.resulting_cents, "activity_id": result.activity.id,
                })
        except AppError as exc:
            db.rollback()
            results.append({"user_id": user_id, "ok": False, "error": exc.message})
    return results


def get_request(db: Session, pending_id: int) -> PendingBalanceChange:
    pending = db.scalar(
        select(PendingBalanceChange)
        .where(PendingBalanceChange.id == pending_id)
        .options(selectinload(PendingBalanceChange.user), selectinload(PendingBalanceChange.requested_by),
                 selectinload(PendingBalanceChange.decided_by), selectinload(PendingBalanceChange.claim))
    )
    if pending is None:
        raise NotFound("Approval request not found.")
    return pending


def _claim_note(db: Session, pending: PendingBalanceChange, actor: User, message: str) -> None:
    if pending.claim_id is None:
        return
    from backend.services import claim_service

    claim = claim_service.get_for_admin(db, pending.claim_id)
    claim_service.add_internal_event(db, claim, actor=actor, message=message)


def _mark_decided(db: Session, pending_id: int, *, status: str, admin: User, note: str | None = None,
                  activity_id: int | None = None) -> None:
    """Conditional update so two administrators cannot decide the same request."""
    decided = db.execute(
        update(PendingBalanceChange)
        .where(PendingBalanceChange.id == pending_id, PendingBalanceChange.status == APPROVAL_PENDING)
        .values(status=status, decided_by_id=admin.id, decided_at=utcnow(), decision_note=note, activity_id=activity_id)
        .execution_options(synchronize_session=False)
    )
    if decided.rowcount != 1:
        raise Conflict("This request has already been decided.", code="already_decided")


def approve(db: Session, *, approver: User, pending_id: int, allow_negative: bool) -> PendingBalanceChange:
    pending = get_request(db, pending_id)
    if pending.status != APPROVAL_PENDING:
        raise Conflict("This request has already been decided.", code="already_decided")
    if pending.requested_by_id == approver.id:
        raise Forbidden("A different administrator must approve your request.", code="self_approval")
    requester = pending.requested_by
    if not requester.is_admin or not requester.is_active:
        raise Conflict("The administrator who requested this change is no longer active. Reject it and request it again.",
                       code="requester_inactive")
    claim_id, claim_reply = pending.claim_id, pending.claim_reply

    def finalize(session: Session, activity: BalanceActivity) -> None:
        _mark_decided(session, pending_id, status=APPROVAL_APPROVED, admin=approver, activity_id=activity.id)
        if claim_id is not None:
            from backend.services import claim_service

            claim_service.finalize_credit(session, claim_id=claim_id, actor=approver, activity=activity, reply=claim_reply)

    balance_service.apply_change(
        db, admin=requester, user_id=pending.user_id, mode=pending.mode, value=pending.value_cents,
        reason=pending.reason, allow_negative=allow_negative, claim_id=claim_id, approved_by=approver,
        before_commit=finalize,
    )
    db.expire_all()
    return get_request(db, pending_id)


def reject(db: Session, *, admin: User, pending_id: int, note: str | None) -> PendingBalanceChange:
    """Reject someone else's request, or cancel your own."""
    pending = get_request(db, pending_id)
    if pending.status != APPROVAL_PENDING:
        raise Conflict("This request has already been decided.", code="already_decided")
    own = pending.requested_by_id == admin.id
    text = " ".join((note or "").split())[:200] or None
    status = APPROVAL_CANCELLED if own else APPROVAL_REJECTED
    _mark_decided(db, pending_id, status=status, admin=admin, note=text)
    verb = "cancelled" if own else "rejected"
    audit_service.record(
        db, action=f"balance.approval_{verb}", actor_id=admin.id, target_user_id=pending.user_id,
        details=f"Request #{pending.id}: {_describe(pending)}" + (f" · {text}" if text else ""),
    )
    _claim_note(db, pending, admin, f"Credit request {verb}" + (f": {text}" if text else "."))
    db.commit()
    db.expire_all()
    return get_request(db, pending_id)


def list_requests(db: Session) -> tuple[list[PendingBalanceChange], list[PendingBalanceChange]]:
    options = (selectinload(PendingBalanceChange.user), selectinload(PendingBalanceChange.requested_by),
               selectinload(PendingBalanceChange.decided_by), selectinload(PendingBalanceChange.claim))
    open_rows = db.scalars(
        select(PendingBalanceChange).where(PendingBalanceChange.status == APPROVAL_PENDING)
        .options(*options).order_by(PendingBalanceChange.created_at.asc(), PendingBalanceChange.id.asc())
    ).all()
    decided = db.scalars(
        select(PendingBalanceChange).where(PendingBalanceChange.status != APPROVAL_PENDING)
        .options(*options).order_by(PendingBalanceChange.decided_at.desc(), PendingBalanceChange.id.desc())
        .limit(RECENT_DECIDED_LIMIT)
    ).all()
    return open_rows, decided


def pending_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(PendingBalanceChange)
                     .where(PendingBalanceChange.status == APPROVAL_PENDING)) or 0


def pending_for_claim(db: Session, claim_id: int) -> PendingBalanceChange | None:
    return db.scalar(select(PendingBalanceChange).where(
        PendingBalanceChange.claim_id == claim_id, PendingBalanceChange.status == APPROVAL_PENDING))
