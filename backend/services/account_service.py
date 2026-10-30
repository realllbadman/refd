"""Accounts: creation, registration, lookup, status, passwords, notes, statistics."""

from __future__ import annotations

import hmac
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from backend.errors import Conflict, Forbidden, NotFound, ValidationFailed
from backend.models import (
    ROLE_ADMIN,
    ROLE_USER,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    AccountNote,
    AuditEntry,
    BalanceActivity,
    RecoveryCode,
    User,
    utcnow,
)
from backend.security import (
    RECOVERY_CODE_COUNT,
    PasswordPolicyError,
    generate_recovery_code,
    generate_temporary_password,
    hash_password,
    hash_recovery_code,
    password_too_long,
    validate_new_password,
    verify_password,
    verify_totp,
)
from backend.services import audit_service

logger = logging.getLogger("ledger")

# Usernames are normalised by trimming whitespace and lower-casing. After
# normalisation they must be 3-32 characters of a-z, 0-9, ".", "_" or "-",
# starting and ending with a letter or digit.
USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$")
RESERVED_USERNAMES = {"admin", "administrator", "root", "support", "system", "staff", "moderator"}
DISPLAY_NAME_MAX = 60
RECENT_ACTIVITY_DAYS = 7
MAX_PAGE_SIZE = 100
SORTS = {
    "newest": (User.created_at.desc(), User.id.desc()),
    "oldest": (User.created_at.asc(), User.id.asc()),
    "balance_desc": (User.balance_cents.desc(), User.id.desc()),
    "balance_asc": (User.balance_cents.asc(), User.id.asc()),
    "username": (User.username.asc(),),
    "last_login": (User.last_login_at.desc(), User.id.desc()),
}


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def validate_username(raw: str) -> str:
    username = normalize_username(raw)
    if not USERNAME_RE.match(username):
        raise ValidationFailed(
            "Username must be 3-32 characters: letters, numbers, '.', '_' or '-', "
            "starting and ending with a letter or number.",
            details=[{"field": "username", "message": "Invalid username."}],
        )
    return username


def clean_display_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = " ".join(raw.split())
    if not value:
        return None
    if len(value) > DISPLAY_NAME_MAX:
        raise ValidationFailed(
            f"Display name must be at most {DISPLAY_NAME_MAX} characters.",
            details=[{"field": "display_name", "message": "Too long."}],
        )
    if any(ord(ch) < 32 for ch in value):
        raise ValidationFailed("Display name contains invalid characters.")
    return value


def _check_password(password: str) -> None:
    try:
        validate_new_password(password)
    except PasswordPolicyError as exc:
        raise ValidationFailed(str(exc), details=[{"field": "password", "message": str(exc)}]) from exc


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("Account not found.")
    return user


def find_by_username(db: Session, raw_username: str) -> User | None:
    return db.scalar(select(User).where(User.username == normalize_username(raw_username)))


@dataclass
class CreatedAccount:
    user: User
    temporary_password: str | None


def create_account(
    db: Session,
    *,
    username: str,
    password: str | None,
    generate_password: bool = False,
    role: str = ROLE_USER,
    display_name: str | None = None,
    signup_source: str = "admin",
    actor: User | None = None,
    ip: str | None = None,
) -> CreatedAccount:
    """Create an active account with a zero balance."""
    username = validate_username(username)
    display = clean_display_name(display_name)
    if generate_password and password:
        raise ValidationFailed("Provide a password or request a generated one, not both.")
    if not generate_password and not password:
        raise ValidationFailed(
            "Enter a password or request a generated one.",
            details=[{"field": "password", "message": "Password is required."}],
        )
    temporary = generate_temporary_password() if generate_password else None
    plain = temporary or password
    _check_password(plain)

    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise Conflict("That username is already taken.", code="username_taken",
                       details=[{"field": "username", "message": "Already taken."}])

    now = utcnow()
    user = User(
        username=username,
        display_name=display,
        password_hash=hash_password(plain),
        role=role,
        status=STATUS_ACTIVE,
        balance_cents=0,
        signup_source=signup_source,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        db.flush()
        if signup_source == "self":
            audit_service.record(db, action="account.registered", actor_id=user.id, target_user_id=user.id, ip=ip)
        else:
            audit_service.record(
                db, action="account.created", actor_id=actor.id if actor else None, target_user_id=user.id,
                details=f"role={role}", ip=ip,
            )
        db.commit()
    except IntegrityError as exc:  # concurrent creation of the same username
        db.rollback()
        raise Conflict("That username is already taken.", code="username_taken") from exc
    db.refresh(user)
    logger.info("Account created: id=%s username=%s role=%s source=%s", user.id, user.username, user.role, signup_source)
    return CreatedAccount(user=user, temporary_password=temporary)


def register_account(db: Session, *, username: str, password: str, display_name: str | None, ip: str | None) -> User:
    """Public self-registration: always an ordinary account holder with a $0.00 balance."""
    if normalize_username(username) in RESERVED_USERNAMES:
        raise Conflict("That username is not available.", code="username_taken",
                       details=[{"field": "username", "message": "Not available."}])
    return create_account(
        db, username=username, password=password, display_name=display_name, signup_source="self", ip=ip
    ).user


def record_login(db: Session, user: User, ip: str | None) -> None:
    user.last_login_at = utcnow()
    audit_service.record(db, action="auth.login", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()


def count_active_admins(db: Session) -> int:
    return db.scalar(
        select(func.count()).select_from(User).where(User.role == ROLE_ADMIN, User.status == STATUS_ACTIVE)
    )


def _apply_status(db: Session, *, actor: User, user: User, status: str) -> bool:
    if user.status == status:
        return False
    if status == STATUS_INACTIVE and user.role == ROLE_ADMIN:
        if user.id == actor.id:
            raise Conflict("You cannot deactivate your own administrator account.", code="self_deactivation")
        if count_active_admins(db) <= 1:
            raise Conflict("At least one active administrator is required.", code="last_admin")
    user.status = status
    user.updated_at = utcnow()
    if status == STATUS_INACTIVE:
        # Invalidate every existing session for this account.
        user.session_version = (user.session_version or 1) + 1
    audit_service.record(
        db, action="account.deactivated" if status == STATUS_INACTIVE else "account.activated",
        actor_id=actor.id, target_user_id=user.id,
    )
    return True


def set_status(db: Session, *, actor: User, user_id: int, status: str) -> User:
    if status not in (STATUS_ACTIVE, STATUS_INACTIVE):
        raise ValidationFailed("Status must be 'active' or 'inactive'.")
    user = get_user(db, user_id)
    if _apply_status(db, actor=actor, user=user, status=status):
        db.commit()
        db.refresh(user)
        logger.info("Account %s status set to %s by admin %s", user.id, status, actor.id)
    return user


def bulk_set_status(db: Session, *, actor: User, user_ids: list[int], status: str) -> list[dict]:
    results = []
    for user_id in user_ids:
        try:
            set_status(db, actor=actor, user_id=user_id, status=status)
            results.append({"user_id": user_id, "ok": True})
        except (Conflict, NotFound, ValidationFailed) as exc:
            db.rollback()
            results.append({"user_id": user_id, "ok": False, "error": exc.message})
    return results


def reset_password(db: Session, *, admin: User, user_id: int) -> tuple[User, str]:
    user = get_user(db, user_id)
    if user.id == admin.id:
        raise Conflict("Use your profile page to change your own password.", code="self_reset")
    temporary = generate_temporary_password()
    user.password_hash = hash_password(temporary)
    user.session_version = (user.session_version or 1) + 1  # sign out everywhere
    user.updated_at = utcnow()
    audit_service.record(db, action="account.password_reset", actor_id=admin.id, target_user_id=user.id)
    db.commit()
    db.refresh(user)
    return user, temporary


def change_password(db: Session, *, user: User, current: str, new: str, ip: str | None = None) -> User:
    if not verify_password(current, user.password_hash):
        raise ValidationFailed("Your current password is incorrect.",
                               details=[{"field": "current_password", "message": "Incorrect password."}])
    if current == new:
        raise ValidationFailed("Choose a password different from your current one.",
                               details=[{"field": "new_password", "message": "Must be different."}])
    try:
        validate_new_password(new)
    except PasswordPolicyError as exc:
        raise ValidationFailed(str(exc), details=[{"field": "new_password", "message": str(exc)}]) from exc
    user.password_hash = hash_password(new)
    user.session_version = (user.session_version or 1) + 1
    user.updated_at = utcnow()
    audit_service.record(db, action="account.password_changed", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    db.refresh(user)
    return user


RECOVERY_FAILED = "That username and recovery code don't match. Check the code, or contact support."
SIGN_IN_HISTORY_LIMIT = 10


def _require_password(user: User, password: str, field: str = "password") -> None:
    if not password or password_too_long(password) or not verify_password(password, user.password_hash):
        raise ValidationFailed("Your password is incorrect.", details=[{"field": field, "message": "Incorrect password."}])


def recovery_codes_remaining(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(RecoveryCode).where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
    ) or 0


def create_recovery_codes(db: Session, *, user: User, password: str, ip: str | None = None) -> list[str]:
    """Replace the account's recovery codes. The plain codes are returned once and never stored."""
    if user.is_admin:
        raise Forbidden("Administrator passwords are reset by another administrator.")
    _require_password(user, password)
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = [generate_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    now = utcnow()
    db.add_all(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(c), created_at=now) for c in codes)
    audit_service.record(db, action="account.recovery_codes_created", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    return codes


def recover_password(db: Session, *, username: str, code: str, new_password: str, ip: str | None = None) -> User:
    """Reset a forgotten password with a one-time recovery code; signs out every device."""
    user = find_by_username(db, username or "")
    digest = hash_recovery_code(code)
    match = None
    if user is not None and not user.is_admin:
        for row in db.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None))):
            if hmac.compare_digest(row.code_hash, digest):
                match = row
    if match is None or not user.is_active:
        raise ValidationFailed(RECOVERY_FAILED, code="recovery_failed")
    try:
        validate_new_password(new_password)
    except PasswordPolicyError as exc:
        raise ValidationFailed(str(exc), details=[{"field": "new_password", "message": str(exc)}]) from exc
    match.used_at = utcnow()
    user.password_hash = hash_password(new_password)
    user.session_version = (user.session_version or 1) + 1
    user.updated_at = utcnow()
    audit_service.record(db, action="account.password_recovered", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    db.refresh(user)
    return user


def close_own_account(db: Session, *, user: User, password: str, ip: str | None = None) -> User:
    """An account holder closes their own account. Balance and history are kept; an admin can reopen it."""
    if user.is_admin:
        raise Forbidden("Administrator accounts are deactivated by another administrator.")
    _require_password(user, password)
    user.status = STATUS_INACTIVE
    user.session_version = (user.session_version or 1) + 1
    user.updated_at = utcnow()
    audit_service.record(db, action="account.closed", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    db.refresh(user)
    logger.info("Account %s closed by its holder", user.id)
    return user


def sign_in_history(db: Session, user_id: int, limit: int = SIGN_IN_HISTORY_LIMIT) -> list[AuditEntry]:
    return db.scalars(
        select(AuditEntry)
        .where(AuditEntry.action == "auth.login", AuditEntry.target_user_id == user_id)
        .order_by(AuditEntry.id.desc()).limit(limit)
    ).all()


# --- two-step sign-in ----------------------------------------------------------------


def enable_totp(db: Session, *, user: User, secret: str, code: str, ip: str | None = None) -> User:
    if not user.is_admin:
        raise Forbidden("Two-step sign-in is available for administrator accounts.")
    if user.totp_secret:
        raise Conflict("Two-step sign-in is already on.", code="totp_enabled")
    step = verify_totp(secret, code)
    if step is None:
        raise ValidationFailed("That code didn't match. Check the time on your device and try again.",
                               details=[{"field": "code", "message": "Incorrect code."}])
    user.totp_secret = secret
    user.totp_last_step = step
    user.updated_at = utcnow()
    audit_service.record(db, action="account.totp_enabled", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    db.refresh(user)
    return user


def check_login_code(db: Session, *, user: User, code: str) -> bool:
    """Verify a sign-in code and burn its time step so it cannot be replayed."""
    step = verify_totp(user.totp_secret or "", code, last_step=user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    db.commit()
    return True


def disable_totp(db: Session, *, user: User, password: str, code: str, ip: str | None = None) -> User:
    if not user.totp_secret:
        raise Conflict("Two-step sign-in is already off.", code="totp_disabled")
    _require_password(user, password)
    if verify_totp(user.totp_secret, code, last_step=user.totp_last_step) is None:
        raise ValidationFailed("That code didn't match.", details=[{"field": "code", "message": "Incorrect code."}])
    user.totp_secret = None
    user.totp_last_step = None
    user.updated_at = utcnow()
    audit_service.record(db, action="account.totp_disabled", actor_id=user.id, target_user_id=user.id, ip=ip)
    db.commit()
    db.refresh(user)
    return user


def reset_totp(db: Session, *, admin: User, user_id: int) -> User:
    """Another administrator turns off two-step sign-in for a colleague who lost their device."""
    user = get_user(db, user_id)
    if user.id == admin.id:
        raise Conflict("Use your own security settings to change two-step sign-in.", code="self_reset")
    if not user.totp_secret:
        raise Conflict("Two-step sign-in is not on for this account.", code="totp_disabled")
    user.totp_secret = None
    user.totp_last_step = None
    user.session_version = (user.session_version or 1) + 1
    user.updated_at = utcnow()
    audit_service.record(db, action="account.totp_reset", actor_id=admin.id, target_user_id=user.id)
    db.commit()
    db.refresh(user)
    return user


def update_profile(db: Session, *, user: User, display_name: str | None) -> User:
    user.display_name = clean_display_name(display_name)
    user.updated_at = utcnow()
    audit_service.record(db, action="account.profile_updated", actor_id=user.id, target_user_id=user.id)
    db.commit()
    db.refresh(user)
    return user


# --- notes --------------------------------------------------------------------------


def list_notes(db: Session, user_id: int):
    get_user(db, user_id)
    return db.scalars(
        select(AccountNote).where(AccountNote.user_id == user_id)
        .options(selectinload(AccountNote.author))
        .order_by(AccountNote.created_at.desc(), AccountNote.id.desc())
    ).all()


def add_note(db: Session, *, admin: User, user_id: int, body: str) -> AccountNote:
    get_user(db, user_id)
    text = (body or "").strip()
    if not text or len(text) > 1000:
        raise ValidationFailed("Note must be 1-1000 characters.", details=[{"field": "body", "message": "Invalid note."}])
    note = AccountNote(user_id=user_id, author_id=admin.id, body=text, created_at=utcnow())
    db.add(note)
    audit_service.record(db, action="account.note_added", actor_id=admin.id, target_user_id=user_id)
    db.commit()
    db.refresh(note)
    return note


def delete_note(db: Session, *, admin: User, user_id: int, note_id: int) -> None:
    note = db.get(AccountNote, note_id)
    if note is None or note.user_id != user_id:
        raise NotFound("Note not found.")
    if note.author_id not in (None, admin.id):
        raise Forbidden("You can only delete your own notes.")
    db.delete(note)
    audit_service.record(db, action="account.note_deleted", actor_id=admin.id, target_user_id=user_id)
    db.commit()


# --- listing ------------------------------------------------------------------------


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _account_filters(q: str, status: str, role: str):
    conditions = []
    term = q.strip()
    if term:
        lowered = term.lower().lstrip("#")
        like = f"%{_escape_like(lowered)}%"
        options = [User.username.like(like, escape="\\"), func.lower(User.display_name).like(like, escape="\\")]
        if lowered.isdigit() and len(lowered) <= 18:
            options.append(User.id == int(lowered))
        conditions.append(or_(*options))
    if status in (STATUS_ACTIVE, STATUS_INACTIVE):
        conditions.append(User.status == status)
    if role in (ROLE_USER, ROLE_ADMIN):
        conditions.append(User.role == role)
    return conditions


def list_accounts(
    db: Session, *, q: str = "", status: str = "", role: str = "", sort: str = "newest", page: int = 1, page_size: int = 20
):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    conditions = _account_filters(q, status, role)
    total = db.scalar(select(func.count()).select_from(User).where(*conditions))
    rows = db.scalars(
        select(User).where(*conditions).order_by(*SORTS.get(sort, SORTS["newest"]))
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return rows, total, page, page_size, max(1, math.ceil(total / page_size))


def export_accounts(db: Session, *, q: str = "", status: str = "", role: str = "", sort: str = "newest"):
    conditions = _account_filters(q, status, role)
    return db.scalars(select(User).where(*conditions).order_by(*SORTS.get(sort, SORTS["newest"]))).all()


# --- statistics ---------------------------------------------------------------------


def admin_stats(db: Session) -> dict:
    from backend.services.message_service import unread_count

    users = select(User).where(User.role == ROLE_USER).subquery()
    since = utcnow() - timedelta(days=RECENT_ACTIVITY_DAYS)
    total, active, balance_sum, new_users = db.execute(
        select(
            func.count(users.c.id),
            func.count(users.c.id).filter(users.c.status == STATUS_ACTIVE),
            func.coalesce(func.sum(users.c.balance_cents), 0),
            func.count(users.c.id).filter(users.c.created_at >= since),
        )
    ).one()
    recent = db.scalar(select(func.count()).select_from(BalanceActivity).where(BalanceActivity.created_at >= since))
    all_activity = db.scalar(select(func.count()).select_from(BalanceActivity))
    return {
        "total_users": total,
        "active_users": active,
        "inactive_users": total - active,
        "new_users_7d": new_users,
        "total_balance_cents": int(balance_sum),
        "average_balance_cents": int(balance_sum) // total if total else 0,
        "recent_activity_count": recent,
        "recent_activity_window_days": RECENT_ACTIVITY_DAYS,
        "total_activity_count": all_activity,
        "unread_messages": unread_count(db),
    }


def overview(db: Session) -> dict:
    recent_signups = db.scalars(
        select(User).where(User.role == ROLE_USER).order_by(User.created_at.desc(), User.id.desc()).limit(6)
    ).all()
    top_balances = db.scalars(
        select(User).where(User.role == ROLE_USER, User.balance_cents > 0)
        .order_by(User.balance_cents.desc(), User.id.asc()).limit(6)
    ).all()
    recent_activity = db.scalars(
        select(BalanceActivity)
        .options(selectinload(BalanceActivity.user), selectinload(BalanceActivity.admin),
                 selectinload(BalanceActivity.approved_by), selectinload(BalanceActivity.claim))
        .order_by(BalanceActivity.id.desc()).limit(8)
    ).all()
    return {"recent_signups": recent_signups, "top_balances": top_balances, "recent_activity": recent_activity}


def chart_series(db: Session, days: int = 30) -> dict:
    """Per-day series (UTC dates) for the last ``days`` days, zero-filled."""
    days = max(7, min(days, 90))
    today = utcnow().date()
    start = today - timedelta(days=days - 1)
    start_dt = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)

    signups = dict(db.execute(
        select(func.date(User.created_at), func.count())
        .where(User.role == ROLE_USER, User.created_at >= start_dt)
        .group_by(func.date(User.created_at))
    ).all())
    flows = {
        row[0]: row[1:]
        for row in db.execute(
            select(
                func.date(BalanceActivity.created_at),
                func.coalesce(func.sum(case((BalanceActivity.amount_cents > 0, BalanceActivity.amount_cents), else_=0)), 0),
                func.coalesce(func.sum(case((BalanceActivity.amount_cents < 0, BalanceActivity.amount_cents), else_=0)), 0),
                func.count(),
            )
            .where(BalanceActivity.created_at >= start_dt)
            .group_by(func.date(BalanceActivity.created_at))
        ).all()
    }
    series = []
    for offset in range(days):
        day: date = start + timedelta(days=offset)
        key = day.isoformat()
        credits, debits, count = flows.get(key, (0, 0, 0))
        series.append({
            "date": key,
            "signups": int(signups.get(key, 0)),
            "credits_cents": int(credits),
            "debits_cents": int(debits),
            "net_cents": int(credits) + int(debits),
            "changes": int(count),
        })
    return {"days": days, "series": series}


def user_summary(db: Session, user: User) -> dict:
    credits, debits, count, first_at = db.execute(
        select(
            func.coalesce(func.sum(case((BalanceActivity.amount_cents > 0, BalanceActivity.amount_cents), else_=0)), 0),
            func.coalesce(func.sum(case((BalanceActivity.amount_cents < 0, BalanceActivity.amount_cents), else_=0)), 0),
            func.count(BalanceActivity.id),
            func.min(BalanceActivity.created_at),
        ).where(BalanceActivity.user_id == user.id)
    ).one()
    points = db.execute(
        select(BalanceActivity.created_at, BalanceActivity.resulting_cents)
        .where(BalanceActivity.user_id == user.id)
        .order_by(BalanceActivity.id.desc()).limit(60)
    ).all()
    history = [{"at": at, "balance_cents": cents} for at, cents in reversed(points)]
    return {
        "balance_cents": user.balance_cents,
        "total_credits_cents": int(credits),
        "total_debits_cents": int(debits),
        "change_count": count,
        "first_activity_at": first_at,
        "history": history,
    }
