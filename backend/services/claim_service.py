"""Refund claim intake, status transitions and review timeline."""

from __future__ import annotations

import math
import os
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from backend.errors import Conflict, NotFound, ValidationFailed
from backend.models import (
    APPROVAL_CANCELLED,
    APPROVAL_PENDING,
    CHANGE_ADJUST,
    CLAIM_APPROVED,
    CLAIM_AWAITING_CUSTOMER,
    CLAIM_CANCELLED,
    CLAIM_DECLINED,
    CLAIM_REVIEWING,
    CLAIM_STATUSES,
    CLAIM_SUBMITTED,
    ROLE_ADMIN,
    BalanceActivity,
    PendingBalanceChange,
    RefundClaim,
    RefundClaimEvidence,
    RefundClaimEvent,
    User,
    utcnow,
)
from backend.money import format_cents
from backend.services import audit_service, settings_service

MAX_PAGE_SIZE = 100
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_PER_CLAIM = 5
REASONS = {"not_received", "damaged", "incorrect_item", "duplicate_charge", "cancelled", "other"}
TERMINAL_STATUSES = {CLAIM_APPROVED, CLAIM_DECLINED, CLAIM_CANCELLED}
OPEN_STATUSES = (CLAIM_SUBMITTED, CLAIM_REVIEWING, CLAIM_AWAITING_CUSTOMER)
OVERDUE_AFTER = timedelta(hours=48)
QUEUES = {"", "mine", "unassigned", "overdue", "stale"}
REASON_LABELS = {
    "not_received": "Order not received",
    "damaged": "Item arrived damaged",
    "incorrect_item": "Incorrect item",
    "duplicate_charge": "Duplicate charge",
    "cancelled": "Order cancelled",
    "other": "Other",
}
EVIDENCE_TYPES = {
    "application/pdf": (b"%PDF-", ".pdf"),
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/webp": (b"RIFF", ".webp"),
}


def _clean(value: str | None, field: str, *, required: bool = True, limit: int) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        if required:
            raise ValidationFailed(f"Enter {field}.", details=[{"field": field, "message": "Required."}])
        return None
    if len(text) > limit:
        raise ValidationFailed(f"{field.capitalize()} must be at most {limit} characters.")
    return text


def _event(
    db: Session,
    claim: RefundClaim,
    *,
    actor: User | None,
    event_type: str,
    message: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    is_internal: bool = False,
) -> None:
    db.add(RefundClaimEvent(
        claim=claim,
        actor_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        message=message,
        is_internal=is_internal,
        created_at=utcnow(),
    ))


def _detect_evidence_type(data: bytes, declared_type: str | None) -> tuple[str, str]:
    detected = None
    for content_type, (signature, extension) in EVIDENCE_TYPES.items():
        if data.startswith(signature) and (content_type != "image/webp" or data[8:12] == b"WEBP"):
            detected = (content_type, extension)
            break
    if detected is None:
        raise ValidationFailed("Upload a PDF, JPEG, PNG or WebP file.")
    if declared_type and declared_type not in (detected[0], "application/octet-stream"):
        raise ValidationFailed("The file type does not match the uploaded content.")
    return detected


def _safe_original_filename(filename: str | None) -> str:
    leaf = (filename or "evidence").replace("\\", "/").split("/")[-1]
    clean = "".join(char for char in leaf if char.isprintable() and char not in '<>:\"|?*')
    clean = clean.strip().strip(".")[:255]
    return clean or "evidence"


def create(
    db: Session,
    *,
    user: User,
    merchant: str,
    order_reference: str,
    purchase_date,
    amount_cents: int,
    reason: str,
    description: str,
) -> RefundClaim:
    if amount_cents <= 0:
        raise ValidationFailed("Claim amount must be greater than zero.", details=[{"field": "amount_cents", "message": "Enter an amount above zero."}])
    if reason not in REASONS:
        raise ValidationFailed("Choose a valid claim reason.")
    claim = RefundClaim(
        user_id=user.id,
        merchant=_clean(merchant, "merchant", limit=120),
        order_reference=_clean(order_reference, "order reference", limit=80),
        purchase_date=purchase_date,
        amount_cents=amount_cents,
        reason=reason,
        description=_clean(description, "description", limit=4000),
        status=CLAIM_SUBMITTED,
        priority="normal",
        created_at=utcnow(),
        updated_at=utcnow(),
        status_changed_at=utcnow(),
    )
    db.add(claim)
    db.flush()
    _event(db, claim, actor=user, event_type="submitted", to_status=CLAIM_SUBMITTED)
    audit_service.record(
        db, action="claim.created", actor_id=user.id, target_user_id=user.id,
        details=f"Claim #{claim.id} · {claim.merchant[:80]}",
    )
    db.commit()
    return get_for_user(db, claim_id=claim.id, user_id=user.id)


def _load_claim(db: Session, claim_id: int) -> RefundClaim | None:
    return db.scalar(
        select(RefundClaim)
        .where(RefundClaim.id == claim_id)
        .options(
            selectinload(RefundClaim.user),
            selectinload(RefundClaim.assigned_admin),
            selectinload(RefundClaim.events).selectinload(RefundClaimEvent.actor),
            selectinload(RefundClaim.evidence),
        )
    )


def get_for_user(db: Session, *, claim_id: int, user_id: int) -> RefundClaim:
    claim = _load_claim(db, claim_id)
    if claim is None or claim.user_id != user_id:
        raise NotFound("Claim not found.")
    return claim


def get_for_admin(db: Session, claim_id: int) -> RefundClaim:
    claim = _load_claim(db, claim_id)
    if claim is None:
        raise NotFound("Claim not found.")
    return claim


def add_evidence(
    db: Session,
    *,
    claim_id: int,
    user: User,
    data: bytes,
    filename: str | None,
    declared_type: str | None,
    upload_dir: Path,
) -> RefundClaimEvidence:
    claim = get_for_user(db, claim_id=claim_id, user_id=user.id)
    if claim.status in TERMINAL_STATUSES:
        raise ValidationFailed("Evidence cannot be added to a closed claim.")
    if not data or len(data) > MAX_EVIDENCE_BYTES:
        raise ValidationFailed("Each file must be between 1 byte and 8 MB.")
    if len(claim.evidence) >= MAX_EVIDENCE_PER_CLAIM:
        raise ValidationFailed("A claim can have at most five evidence files.")
    content_type, extension = _detect_evidence_type(data, declared_type)
    original_filename = _safe_original_filename(filename)
    stored_filename = secrets.token_hex(32) + extension
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(upload_dir, 0o700)
    except OSError:
        pass
    path = upload_dir / stored_filename
    try:
        with path.open("xb") as output:
            output.write(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        evidence = RefundClaimEvidence(
            claim_id=claim.id,
            uploaded_by_id=user.id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            content_type=content_type,
            size_bytes=len(data),
            created_at=utcnow(),
        )
        claim.evidence.append(evidence)
        db.add(evidence)
        _event(db, claim, actor=user, event_type="evidence_added", message=original_filename)
        claim.updated_at = utcnow()
        audit_service.record(
            db, action="claim.evidence_added", actor_id=user.id, target_user_id=user.id,
            details=f"Claim #{claim.id} · {original_filename[:80]}",
        )
        db.commit()
        db.refresh(evidence)
        return evidence
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise


def get_evidence_for_user(db: Session, *, claim_id: int, evidence_id: int, user_id: int):
    claim = db.scalar(
        select(RefundClaim)
        .where(RefundClaim.id == claim_id, RefundClaim.user_id == user_id)
        .options(selectinload(RefundClaim.evidence))
    )
    if claim is None:
        raise NotFound("Claim not found.")
    evidence = next((item for item in claim.evidence if item.id == evidence_id), None)
    if evidence is None:
        raise NotFound("Evidence not found.")
    return evidence


def get_evidence_for_admin(db: Session, *, claim_id: int, evidence_id: int):
    claim = db.scalar(
        select(RefundClaim)
        .where(RefundClaim.id == claim_id)
        .options(selectinload(RefundClaim.evidence))
    )
    if claim is None:
        raise NotFound("Claim not found.")
    evidence = next((item for item in claim.evidence if item.id == evidence_id), None)
    if evidence is None:
        raise NotFound("Evidence not found.")
    return evidence


def evidence_path(evidence: RefundClaimEvidence, *, upload_dir: Path) -> Path:
    path = (upload_dir / evidence.stored_filename).resolve()
    if path.parent != upload_dir.resolve() or not path.is_file():
        raise NotFound("Evidence file not found.")
    return path


def list_for_user(db: Session, user_id: int) -> list[RefundClaim]:
    return db.scalars(
        select(RefundClaim)
        .where(RefundClaim.user_id == user_id)
        .options(
            selectinload(RefundClaim.user),
            selectinload(RefundClaim.assigned_admin),
            selectinload(RefundClaim.events).selectinload(RefundClaimEvent.actor),
            selectinload(RefundClaim.evidence),
        )
        .order_by(RefundClaim.updated_at.desc(), RefundClaim.id.desc())
    ).all()


def _status_since():
    return func.coalesce(RefundClaim.status_changed_at, RefundClaim.created_at)


def _queue_condition(db: Session, queue: str, admin: User | None):
    now = utcnow()
    if queue == "mine":
        return and_(RefundClaim.assigned_admin_id == (admin.id if admin else -1), RefundClaim.status.in_(OPEN_STATUSES))
    if queue == "unassigned":
        return and_(RefundClaim.assigned_admin_id.is_(None), RefundClaim.status.in_(OPEN_STATUSES))
    if queue == "overdue":
        return and_(RefundClaim.status.in_(OPEN_STATUSES), RefundClaim.updated_at < now - OVERDUE_AFTER)
    if queue == "stale":
        cutoff = now - timedelta(days=settings_service.stale_claim_days(db))
        return and_(RefundClaim.status == CLAIM_AWAITING_CUSTOMER, _status_since() < cutoff)
    return None


def list_for_admin(
    db: Session,
    *,
    status: str = "",
    priority: str = "",
    q: str = "",
    queue: str = "",
    admin: User | None = None,
    page: int = 1,
    page_size: int = 20,
):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    conditions = []
    if queue not in QUEUES:
        raise ValidationFailed("Choose a valid queue.")
    queue_condition = _queue_condition(db, queue, admin)
    if queue_condition is not None:
        conditions.append(queue_condition)
    if status:
        if status not in CLAIM_STATUSES:
            raise ValidationFailed("Choose a valid claim status.")
        conditions.append(RefundClaim.status == status)
    if priority:
        if priority not in ("normal", "high", "urgent"):
            raise ValidationFailed("Choose a valid priority.")
        conditions.append(RefundClaim.priority == priority)
    if q.strip():
        like = f"%{q.strip()}%"
        conditions.append(or_(
            RefundClaim.merchant.like(like),
            RefundClaim.order_reference.like(like),
            RefundClaim.description.like(like),
            RefundClaim.user.has(User.username.like(like)),
        ))
    total = db.scalar(select(func.count()).select_from(RefundClaim).where(*conditions))
    rows = db.scalars(
        select(RefundClaim)
        .where(*conditions)
        .options(
            selectinload(RefundClaim.user),
            selectinload(RefundClaim.assigned_admin),
            selectinload(RefundClaim.evidence),
        )
        .order_by(RefundClaim.created_at.desc(), RefundClaim.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return rows, total, page, page_size, max(1, math.ceil(total / page_size))


def open_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(RefundClaim).where(RefundClaim.status.not_in(TERMINAL_STATUSES))) or 0


def queue_counts(db: Session, admin: User) -> dict:
    return {
        queue: db.scalar(select(func.count()).select_from(RefundClaim).where(_queue_condition(db, queue, admin))) or 0
        for queue in ("mine", "unassigned", "overdue", "stale")
    }


def list_assignees(db: Session) -> list[User]:
    return db.scalars(
        select(User)
        .where(User.role == ROLE_ADMIN, User.status == "active")
        .order_by(User.username.asc())
    ).all()


def metrics(db: Session) -> dict:
    counts = dict(db.execute(select(RefundClaim.status, func.count()).group_by(RefundClaim.status)).all())
    open_statuses = OPEN_STATUSES
    now = utcnow()
    overdue_before = now - OVERDUE_AFTER
    open_total = sum(counts.get(status, 0) for status in open_statuses)
    urgent_total = db.scalar(
        select(func.count()).select_from(RefundClaim).where(
            RefundClaim.status.in_(open_statuses), RefundClaim.priority == "urgent"
        )
    ) or 0
    overdue_total = db.scalar(
        select(func.count()).select_from(RefundClaim).where(
            RefundClaim.status.in_(open_statuses), RefundClaim.updated_at < overdue_before
        )
    ) or 0
    first_replies = db.execute(
        select(RefundClaim.created_at, func.min(RefundClaimEvent.created_at))
        .join(RefundClaimEvent, RefundClaimEvent.claim_id == RefundClaim.id)
        .where(RefundClaimEvent.event_type == "admin_reply")
        .group_by(RefundClaim.id)
    ).all()
    resolution_events = db.execute(
        select(RefundClaim.created_at, func.min(RefundClaimEvent.created_at))
        .join(RefundClaimEvent, RefundClaimEvent.claim_id == RefundClaim.id)
        .where(RefundClaimEvent.event_type == "status_change", RefundClaimEvent.to_status.in_((CLAIM_APPROVED, CLAIM_DECLINED)))
        .group_by(RefundClaim.id)
    ).all()
    average = lambda rows: round(sum((end - start).total_seconds() / 60 for start, end in rows) / len(rows)) if rows else None
    approved, declined = counts.get(CLAIM_APPROVED, 0), counts.get(CLAIM_DECLINED, 0)
    credited_count, credited_cents = db.execute(
        select(func.count(BalanceActivity.id), func.coalesce(func.sum(BalanceActivity.amount_cents), 0))
        .where(BalanceActivity.claim_id.is_not(None))
    ).one()
    by_reason = [
        {"reason": reason, "label": REASON_LABELS.get(reason, reason), "total": total,
         "approved": int(approved_n or 0), "declined": int(declined_n or 0)}
        for reason, total, approved_n, declined_n in db.execute(
            select(
                RefundClaim.reason,
                func.count(),
                func.sum(case((RefundClaim.status == CLAIM_APPROVED, 1), else_=0)),
                func.sum(case((RefundClaim.status == CLAIM_DECLINED, 1), else_=0)),
            ).group_by(RefundClaim.reason).order_by(func.count().desc(), RefundClaim.reason)
        ).all()
    ]
    merchant_key = func.lower(RefundClaim.merchant)
    top_merchants = [
        {"merchant": name, "total": total, "amount_cents": int(amount or 0)}
        for name, total, amount in db.execute(
            select(func.min(RefundClaim.merchant), func.count(), func.sum(RefundClaim.amount_cents))
            .group_by(merchant_key).order_by(func.count().desc(), merchant_key).limit(5)
        ).all()
    ]
    return {
        "open": open_total,
        "submitted": counts.get(CLAIM_SUBMITTED, 0),
        "awaiting_customer": counts.get(CLAIM_AWAITING_CUSTOMER, 0),
        "urgent": urgent_total,
        "overdue_48h": overdue_total,
        "average_first_reply_minutes": average(first_replies),
        "average_resolution_minutes": average(resolution_events),
        "approval_rate_percent": round(approved * 100 / (approved + declined)) if approved + declined else None,
        "credited_count": credited_count,
        "credited_cents": int(credited_cents),
        "by_reason": by_reason,
        "top_merchants": top_merchants,
    }


def add_customer_message(db: Session, *, claim_id: int, user: User, message: str) -> RefundClaim:
    claim = get_for_user(db, claim_id=claim_id, user_id=user.id)
    if claim.status in TERMINAL_STATUSES:
        raise ValidationFailed("This claim is closed. Contact support if you need more help.")
    text = _clean(message, "message", limit=4000)
    _event(db, claim, actor=user, event_type="customer_message", message=text)
    claim.updated_at = utcnow()
    db.commit()
    return get_for_user(db, claim_id=claim.id, user_id=user.id)


def _cancel_pending_credit(db: Session, claim: RefundClaim, actor: User) -> None:
    """A closed claim must not be credited later: withdraw any credit still awaiting approval."""
    db.execute(
        update(PendingBalanceChange)
        .where(PendingBalanceChange.claim_id == claim.id, PendingBalanceChange.status == APPROVAL_PENDING)
        .values(status=APPROVAL_CANCELLED, decided_by_id=actor.id, decided_at=utcnow(), decision_note="Claim closed")
        .execution_options(synchronize_session=False)
    )


def cancel_by_user(db: Session, *, claim_id: int, user: User) -> RefundClaim:
    claim = get_for_user(db, claim_id=claim_id, user_id=user.id)
    if claim.status in TERMINAL_STATUSES:
        raise ValidationFailed("This claim is already closed.")
    previous = claim.status
    claim.status = CLAIM_CANCELLED
    claim.updated_at = utcnow()
    claim.status_changed_at = utcnow()
    _event(
        db,
        claim,
        actor=user,
        event_type="status_change",
        from_status=previous,
        to_status=CLAIM_CANCELLED,
    )
    _cancel_pending_credit(db, claim, user)
    db.commit()
    return get_for_user(db, claim_id=claim.id, user_id=user.id)


def update_by_admin(
    db: Session,
    *,
    claim_id: int,
    admin: User,
    status: str | None = None,
    priority: str | None = None,
    assign_to_user_id: int | None = None,
    assign_to_me: bool = False,
    public_reply: str | None = None,
    internal_note: str | None = None,
) -> RefundClaim:
    claim = get_for_admin(db, claim_id)
    if status is not None:
        if status not in {CLAIM_REVIEWING, CLAIM_AWAITING_CUSTOMER, CLAIM_APPROVED, CLAIM_DECLINED, CLAIM_CANCELLED}:
            raise ValidationFailed("Choose a valid next status.")
        if claim.status in TERMINAL_STATUSES and status != claim.status:
            raise ValidationFailed("Closed claims cannot be reopened.")
        if status != claim.status:
            previous = claim.status
            claim.status = status
            claim.status_changed_at = utcnow()
            _event(db, claim, actor=admin, event_type="status_change", from_status=previous, to_status=status)
            if status in TERMINAL_STATUSES:
                _cancel_pending_credit(db, claim, admin)
    if priority is not None:
        if priority not in ("normal", "high", "urgent"):
            raise ValidationFailed("Choose a valid priority.")
        claim.priority = priority
    if assign_to_user_id is not None:
        assignee = db.get(User, assign_to_user_id)
        if assignee is None or assignee.role != ROLE_ADMIN or not assignee.is_active:
            raise ValidationFailed("Choose an active administrator for assignment.")
        claim.assigned_admin_id = assignee.id
    elif assign_to_me:
        claim.assigned_admin_id = admin.id
    if public_reply is not None:
        text = _clean(public_reply, "reply", limit=4000)
        _event(db, claim, actor=admin, event_type="admin_reply", message=text)
    if internal_note is not None:
        text = _clean(internal_note, "internal note", limit=4000)
        _event(db, claim, actor=admin, event_type="admin_note", message=text, is_internal=True)
    claim.updated_at = utcnow()
    audit_service.record(
        db, action="claim.updated", actor_id=admin.id, target_user_id=claim.user_id,
        details=f"Claim #{claim.id} · status {claim.status} · priority {claim.priority}",
    )
    db.commit()
    return get_for_admin(db, claim.id)

# --- approve and credit ------------------------------------------------------------


def add_internal_event(db: Session, claim: RefundClaim, *, actor: User | None, message: str) -> None:
    """Private review note added to the caller's transaction (the caller commits)."""
    _event(db, claim, actor=actor, event_type="admin_note", message=message[:4000], is_internal=True)
    claim.updated_at = utcnow()


def finalize_credit(db: Session, *, claim_id: int, actor: User, activity: BalanceActivity, reply: str | None) -> None:
    """Close a claim as approved inside the balance engine's transaction (see ``approve_with_credit``)."""
    claim = db.get(RefundClaim, claim_id)
    if claim is None:
        raise NotFound("Claim not found.")
    if claim.status in TERMINAL_STATUSES:
        raise Conflict("This claim is already closed.", code="claim_closed")
    if activity.user_id != claim.user_id:
        raise Conflict("The credit must go to the account that submitted the claim.", code="claim_account_mismatch")
    previous = claim.status
    now = utcnow()
    claim.status = CLAIM_APPROVED
    claim.status_changed_at = now
    claim.updated_at = now
    _event(db, claim, actor=actor, event_type="status_change", from_status=previous, to_status=CLAIM_APPROVED,
           message=f"Approved with a credit of {format_cents(activity.amount_cents)} to your account balance.")
    if reply:
        _event(db, claim, actor=actor, event_type="admin_reply", message=reply)
    audit_service.record(
        db, action="claim.updated", actor_id=actor.id, target_user_id=claim.user_id,
        details=f"Claim #{claim.id} · approved · credited {format_cents(activity.amount_cents)}",
    )


def approve_with_credit(
    db: Session,
    *,
    claim_id: int,
    admin: User,
    credit_cents: int,
    public_reply: str | None,
    allow_negative: bool,
) -> tuple[RefundClaim, PendingBalanceChange | None]:
    """Approve a claim and credit the account in one transaction.

    At or above the approval threshold the credit is queued for a second
    administrator instead; the claim then stays open until it is approved.
    """
    from backend.services import approval_service, balance_service

    claim = get_for_admin(db, claim_id)
    if claim.status in TERMINAL_STATUSES:
        raise ValidationFailed("Closed claims cannot be credited.")
    if not 0 < credit_cents <= claim.amount_cents:
        raise ValidationFailed(
            f"Credit must be between $0.01 and the claimed {format_cents(claim.amount_cents)}.",
            details=[{"field": "credit_cents", "message": "Out of range."}],
        )
    if db.scalar(select(BalanceActivity.id).where(BalanceActivity.claim_id == claim.id)) is not None:
        raise Conflict("This claim has already been credited.", code="claim_already_credited")
    if approval_service.pending_for_claim(db, claim.id) is not None:
        raise Conflict("A credit for this claim is already waiting for approval.", code="claim_credit_pending")
    reply = _clean(public_reply, "reply", required=False, limit=4000)
    reason = f"Refund for claim #{claim.id} · {claim.merchant}"[:200]
    previous, _ = balance_service.preview(db, user_id=claim.user_id, mode=CHANGE_ADJUST, value=credit_cents,
                                          allow_negative=allow_negative)

    if approval_service.needs_approval(db, mode=CHANGE_ADJUST, value=credit_cents, previous=previous):
        pending = approval_service.request_change(
            db, admin=admin, user_id=claim.user_id, mode=CHANGE_ADJUST, value=credit_cents, reason=reason,
            allow_negative=allow_negative, claim_id=claim.id, claim_reply=reply, commit=False,
        )
        add_internal_event(db, claim, actor=admin,
                           message=f"Credit of {format_cents(credit_cents)} sent for second-administrator approval (request #{pending.id}).")
        db.commit()
        return get_for_admin(db, claim.id), pending

    target_id = claim.id
    balance_service.apply_change(
        db, admin=admin, user_id=claim.user_id, mode=CHANGE_ADJUST, value=credit_cents, reason=reason,
        allow_negative=allow_negative, claim_id=target_id,
        before_commit=lambda session, activity: finalize_credit(
            session, claim_id=target_id, actor=admin, activity=activity, reply=reply),
    )
    db.expire_all()
    return get_for_admin(db, target_id), None


# --- annotations shown in lists and detail views ---------------------------------------


def _duplicate_groups(db: Session, claims: list[RefundClaim]) -> dict[int, list[RefundClaim]]:
    """Other claims with the same merchant and order reference (case-insensitive)."""
    if not claims:
        return {}
    merchant, order = func.lower(RefundClaim.merchant), func.lower(RefundClaim.order_reference)
    pairs = or_(*[and_(merchant == func.lower(c.merchant), order == func.lower(c.order_reference)) for c in claims])
    rows = db.execute(
        select(RefundClaim, merchant, order).where(pairs).options(selectinload(RefundClaim.user))
        .order_by(RefundClaim.created_at.asc(), RefundClaim.id.asc())
    ).all()
    by_key: dict[tuple[str, str], list[RefundClaim]] = {}
    key_of: dict[int, tuple[str, str]] = {}
    for claim, m, o in rows:
        by_key.setdefault((m, o), []).append(claim)
        key_of[claim.id] = (m, o)
    return {c.id: [other for other in by_key.get(key_of.get(c.id), []) if other.id != c.id] for c in claims}


def annotate(db: Session, claims: list[RefundClaim], *, for_admin: bool, with_duplicates: bool = False) -> dict[int, dict]:
    """Derived per-claim values: credit, pending approval, aging and (admin) duplicates."""
    ids = [c.id for c in claims]
    if not ids:
        return {}
    credits = {
        claim_id: (activity_id, amount)
        for claim_id, activity_id, amount in db.execute(
            select(BalanceActivity.claim_id, BalanceActivity.id, BalanceActivity.amount_cents)
            .where(BalanceActivity.claim_id.in_(ids))
        ).all()
    }
    pending = {
        claim_id: (request_id, value)
        for claim_id, request_id, value in db.execute(
            select(PendingBalanceChange.claim_id, PendingBalanceChange.id, PendingBalanceChange.value_cents)
            .where(PendingBalanceChange.claim_id.in_(ids), PendingBalanceChange.status == APPROVAL_PENDING)
        ).all()
    }
    duplicates = _duplicate_groups(db, claims) if for_admin else {}
    now = utcnow()
    stale_before = now - timedelta(days=settings_service.stale_claim_days(db))
    out = {}
    for claim in claims:
        since = claim.status_changed_at or claim.created_at
        is_open = claim.status in OPEN_STATUSES
        credit = credits.get(claim.id)
        request = pending.get(claim.id)
        extra = {
            "status_since": since,
            "credit_cents": credit[1] if credit else None,
            "credit_activity_id": credit[0] if credit else None,
            "pending_credit_cents": request[1] if for_admin and request else None,
            "pending_request_id": request[0] if for_admin and request else None,
        }
        if for_admin:
            others = duplicates.get(claim.id, [])
            extra.update({
                "overdue": is_open and claim.updated_at < now - OVERDUE_AFTER,
                "stale": claim.status == CLAIM_AWAITING_CUSTOMER and since < stale_before,
                "possible_duplicate": bool(others),
            })
            if with_duplicates:
                extra["duplicates"] = [{
                    "id": other.id, "user_id": other.user_id,
                    "username": other.user.username if other.user else None,
                    "status": other.status, "amount_cents": other.amount_cents, "created_at": other.created_at,
                } for other in others]
        else:
            seen = claim.customer_seen_at
            extra["has_update"] = any(
                not event.is_internal and event.actor_id != claim.user_id and event.event_type != "submitted"
                and (seen is None or event.created_at > seen)
                for event in claim.events
            )
        out[claim.id] = extra
    return out


# --- stale claims -----------------------------------------------------------------------


def close_stale(db: Session, *, admin: User) -> int:
    """Cancel "awaiting customer" claims with no change of status for the configured number of days."""
    days = settings_service.stale_claim_days(db)
    cutoff = utcnow() - timedelta(days=days)
    claims = db.scalars(
        select(RefundClaim).where(RefundClaim.status == CLAIM_AWAITING_CUSTOMER, _status_since() < cutoff)
    ).all()
    now = utcnow()
    message = (f"Closed because we did not hear back within {days} days. "
               "You can start a new claim at any time if you still need help.")
    for claim in claims:
        claim.status = CLAIM_CANCELLED
        claim.status_changed_at = now
        claim.updated_at = now
        _event(db, claim, actor=admin, event_type="status_change", from_status=CLAIM_AWAITING_CUSTOMER,
               to_status=CLAIM_CANCELLED, message=message)
        _cancel_pending_credit(db, claim, admin)
    if claims:
        audit_service.record(db, action="claim.closed_stale", actor_id=admin.id,
                             details=f"{len(claims)} claim(s) awaiting customer for over {days} days: "
                                     + ", ".join(f"#{c.id}" for c in claims[:20]))
    db.commit()
    return len(claims)


# --- account-holder edits and read state ---------------------------------------------------


def update_by_user(db: Session, *, claim_id: int, user: User, merchant: str, order_reference: str, purchase_date,
                   amount_cents: int, reason: str, description: str) -> RefundClaim:
    """Let the account holder correct their claim while the team is waiting on them."""
    claim = get_for_user(db, claim_id=claim_id, user_id=user.id)
    if claim.status != CLAIM_AWAITING_CUSTOMER:
        raise ValidationFailed("You can edit a claim only when the team has asked you for more information.")
    if amount_cents <= 0:
        raise ValidationFailed("Claim amount must be greater than zero.",
                               details=[{"field": "amount_cents", "message": "Enter an amount above zero."}])
    if reason not in REASONS:
        raise ValidationFailed("Choose a valid claim reason.")
    new = {
        "merchant": _clean(merchant, "merchant", limit=120),
        "order_reference": _clean(order_reference, "order reference", limit=80),
        "purchase_date": purchase_date,
        "amount_cents": amount_cents,
        "reason": reason,
        "description": _clean(description, "description", limit=4000),
    }
    labels = {"merchant": "Merchant", "order_reference": "Order reference", "purchase_date": "Purchase date",
              "amount_cents": "Amount", "reason": "Reason", "description": "Description"}
    shown = {
        "amount_cents": format_cents,
        "reason": lambda value: REASON_LABELS.get(value, value),
        "purchase_date": lambda value: value.isoformat(),
    }
    changes = []
    for field, value in new.items():
        old = getattr(claim, field)
        if old == value:
            continue
        if field == "description":
            changes.append("Description updated")
        else:
            fmt = shown.get(field, str)
            changes.append(f"{labels[field]}: {fmt(old)} → {fmt(value)}")
        setattr(claim, field, value)
    if not changes:
        raise ValidationFailed("Nothing changed.")
    _event(db, claim, actor=user, event_type="customer_message", message="Updated claim details. " + "; ".join(changes))
    claim.updated_at = utcnow()
    audit_service.record(db, action="claim.edited", actor_id=user.id, target_user_id=user.id,
                         details=f"Claim #{claim.id} · " + "; ".join(changes)[:200])
    db.commit()
    return get_for_user(db, claim_id=claim.id, user_id=user.id)


def mark_seen_for_user(db: Session, user_id: int) -> None:
    db.execute(update(RefundClaim).where(RefundClaim.user_id == user_id).values(customer_seen_at=utcnow())
               .execution_options(synchronize_session=False))
    db.commit()


def updates_count_for_user(db: Session, user_id: int) -> int:
    """Claims with a team update (reply or status change) the account holder has not seen yet."""
    return db.scalar(
        select(func.count(func.distinct(RefundClaim.id)))
        .join(RefundClaimEvent, RefundClaimEvent.claim_id == RefundClaim.id)
        .where(
            RefundClaim.user_id == user_id,
            RefundClaimEvent.is_internal.is_(False),
            RefundClaimEvent.event_type != "submitted",
            or_(RefundClaimEvent.actor_id.is_(None), RefundClaimEvent.actor_id != user_id),
            or_(RefundClaim.customer_seen_at.is_(None), RefundClaimEvent.created_at > RefundClaim.customer_seen_at),
        )
    ) or 0
