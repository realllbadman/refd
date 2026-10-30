"""Balance engine: validated, atomic, audited balance changes.

Every successful change updates ``users.balance_cents`` and inserts exactly one
``balance_activity`` row in the same database transaction. Concurrency is
handled with an optimistic compare-and-swap UPDATE (``WHERE balance_cents =
<value just read>``): if another transaction changed the balance in between,
the update matches no row and the operation is retried against the fresh value.
The row's write lock is taken before the balance is read, so in practice
concurrent requests for one account are serialised and the guard never trips.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.errors import AppError, Conflict, NotFound, ValidationFailed
from backend.models import CHANGE_ADJUST, CHANGE_SET, ROLE_USER, BalanceActivity, User, utcnow
from backend.money import MAX_AMOUNT_CENTS, format_cents
from backend.services import audit_service

logger = logging.getLogger("ledger")

MAX_ATTEMPTS = 8
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class BalanceBusy(AppError):
    status_code = 409
    code = "balance_busy"


@dataclass
class BalanceResult:
    user: User
    activity: BalanceActivity
    replayed: bool = False


def validate_idempotency_key(key: str | None) -> str | None:
    if key is None or key == "":
        return None
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise ValidationFailed("Idempotency-Key must be 8-64 characters of letters, digits, '-' or '_'.")
    return key


def _compute(mode: str, value: int, previous: int, allow_negative: bool) -> int:
    if abs(value) > MAX_AMOUNT_CENTS:
        raise ValidationFailed(f"Amount must not exceed {format_cents(MAX_AMOUNT_CENTS)}.")
    if mode == CHANGE_SET:
        resulting = value
        if resulting == previous:
            raise ValidationFailed(f"The balance is already {format_cents(previous)}.", code="no_change")
    elif mode == CHANGE_ADJUST:
        if value == 0:
            raise ValidationFailed("Adjustment amount must not be zero.")
        resulting = previous + value
    else:
        raise ValidationFailed("Mode must be 'set' or 'adjust'.")
    if abs(resulting) > MAX_AMOUNT_CENTS:
        raise ValidationFailed(f"Resulting balance must not exceed {format_cents(MAX_AMOUNT_CENTS)}.", code="out_of_range")
    if resulting < 0 and not allow_negative:
        raise Conflict(
            f"This change would make the balance {format_cents(resulting)}. Negative balances are not permitted.",
            code="negative_balance",
        )
    return resulting


def preview(db: Session, *, user_id: int, mode: str, value: int, allow_negative: bool) -> tuple[int, int]:
    """Validate a change against the current balance without applying it: (previous, resulting)."""
    row = db.execute(select(User.balance_cents, User.role).where(User.id == user_id)).one_or_none()
    if row is None:
        raise NotFound("Account not found.")
    previous, role = row
    if role != ROLE_USER:
        raise Conflict("Balances can only be managed for customer accounts.", code="not_customer_account")
    return previous, _compute(mode, value, previous, allow_negative)


def _find_replay(db: Session, admin_id: int, key: str) -> BalanceActivity | None:
    return db.scalar(
        select(BalanceActivity).where(BalanceActivity.admin_id == admin_id, BalanceActivity.idempotency_key == key)
    )


def _replay(db: Session, existing: BalanceActivity, user_id: int, mode: str, value: int, reason: str | None) -> BalanceResult:
    requested_matches = (
        existing.user_id == user_id
        and existing.change_type == mode
        and existing.reason == reason
        and (existing.amount_cents == value if mode == CHANGE_ADJUST else existing.resulting_cents == value)
    )
    if not requested_matches:
        raise Conflict(
            "This Idempotency-Key was already used for a different balance operation.", code="idempotency_mismatch"
        )
    user = db.get(User, user_id)
    db.refresh(user)
    return BalanceResult(user=user, activity=existing, replayed=True)


def apply_change(
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
    approved_by: User | None = None,
    before_commit: Callable[[Session, BalanceActivity], None] | None = None,
) -> BalanceResult:
    """Set (``mode="set"``, ``value`` = target cents) or adjust (``mode="adjust"``,
    ``value`` = signed delta cents) an account balance atomically.

    ``claim_id`` links the activity row to the refund claim it credits (at most
    once per claim). ``before_commit`` runs inside the same transaction after the
    activity row is flushed, so related records commit or roll back with it."""
    if not admin.is_admin or not admin.is_active:
        raise AppError("Administrator privileges required.", code="forbidden", status_code=403)
    if approved_by is not None and (not approved_by.is_admin or not approved_by.is_active):
        raise AppError("Administrator privileges required.", code="forbidden", status_code=403)
    key = validate_idempotency_key(idempotency_key)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if key:
                existing = _find_replay(db, admin.id, key)
                if existing is not None:
                    return _replay(db, existing, user_id, mode, value, reason)

            # Take the row's write lock *before* reading the balance (SQLite has no
            # SELECT ... FOR UPDATE; a no-op UPDATE opens the write transaction).
            # Concurrent changes to this account now queue instead of racing.
            db.execute(
                update(User)
                .where(User.id == user_id)
                .values(balance_cents=User.balance_cents, updated_at=User.updated_at)
                .execution_options(synchronize_session=False)
            )
            row = db.execute(select(User.balance_cents, User.role).where(User.id == user_id)).one_or_none()
            if row is None:
                raise NotFound("Account not found.")
            previous, role = row
            if role != ROLE_USER:
                raise Conflict("Balances can only be managed for customer accounts.", code="not_customer_account")

            resulting = _compute(mode, value, previous, allow_negative)
            now = utcnow()

            swapped = db.execute(
                update(User)
                .where(User.id == user_id, User.balance_cents == previous)
                .values(balance_cents=resulting, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if swapped.rowcount != 1:
                # Should not happen while holding the lock; retry defensively.
                db.rollback()
                continue

            activity = BalanceActivity(
                user_id=user_id,
                admin_id=admin.id,
                change_type=mode,
                previous_cents=previous,
                amount_cents=resulting - previous,
                resulting_cents=resulting,
                reason=reason,
                idempotency_key=key,
                claim_id=claim_id,
                approved_by_id=approved_by.id if approved_by else None,
                created_at=now,
            )
            db.add(activity)
            db.flush()
            audit_service.record(
                db,
                action=f"balance.{mode}",
                actor_id=admin.id,
                target_user_id=user_id,
                details=f"{format_cents(previous)} -> {format_cents(resulting)} ({format_cents(resulting - previous, signed=True)})"
                + (f" · {reason}" if reason else "")
                + (f" · approved by {approved_by.username}" if approved_by else ""),
            )
            if before_commit is not None:
                before_commit(db, activity)
            db.commit()
        except IntegrityError:
            db.rollback()
            if key and _find_replay(db, admin.id, key) is not None:
                continue  # a concurrent request with the same key won; replay it
            if claim_id is not None and db.scalar(
                select(BalanceActivity.id).where(BalanceActivity.claim_id == claim_id)
            ) is not None:
                raise Conflict("This claim has already been credited.", code="claim_already_credited")
            raise
        except OperationalError as exc:
            db.rollback()
            if "locked" in str(exc).lower() and attempt < MAX_ATTEMPTS:
                continue
            logger.exception("Database error during balance change for user %s", user_id)
            raise BalanceBusy("The account is busy. Please try again.") from exc
        except Exception:
            db.rollback()
            raise

        user = db.get(User, user_id)
        db.refresh(user)
        logger.info(
            "Balance %s for user %s by admin %s: %s -> %s (activity %s)",
            mode, user_id, admin.id, previous, resulting, activity.id,
        )
        return BalanceResult(user=user, activity=activity)

    raise BalanceBusy("The balance changed repeatedly while processing. Please review and try again.")


def bulk_adjust(
    db: Session, *, admin: User, user_ids: list[int], amount_cents: int, reason: str | None, allow_negative: bool
) -> list[dict]:
    """Apply the same adjustment to several accounts.

    Each account is its own atomic transaction; the result reports every
    account individually so a rejection (e.g. a negative balance) is visible.
    """
    results = []
    for user_id in user_ids:
        try:
            result = apply_change(
                db, admin=admin, user_id=user_id, mode=CHANGE_ADJUST, value=amount_cents,
                reason=reason, allow_negative=allow_negative,
            )
            results.append({
                "user_id": user_id, "ok": True, "username": result.user.username,
                "resulting_cents": result.activity.resulting_cents, "activity_id": result.activity.id,
            })
        except AppError as exc:
            results.append({"user_id": user_id, "ok": False, "error": exc.message})
    return results
