"""Contact-form and support messages, and administrator replies."""

from __future__ import annotations

import math

from sqlalchemy import func, or_, select, update as sql_update
from sqlalchemy.orm import Session, selectinload

from backend.errors import NotFound, ValidationFailed
from backend.models import MESSAGE_ARCHIVED, MESSAGE_NEW, MESSAGE_READ, ContactMessage, User, utcnow
from backend.services import audit_service, mail_service

LIMITS = {"name": 80, "email": 120, "subject": 120, "body": 4000, "reply": 4000}
MAX_PAGE_SIZE = 100


def _clean(value: str | None, field: str, *, required: bool, multiline: bool = False) -> str | None:
    value = (value or "").strip()
    if not multiline:
        value = " ".join(value.split())
    if not value:
        if required:
            raise ValidationFailed(f"Please enter a {field}.", details=[{"field": field, "message": "Required."}])
        return None
    if len(value) > LIMITS[field]:
        raise ValidationFailed(
            f"{field.capitalize()} must be at most {LIMITS[field]} characters.",
            details=[{"field": field, "message": "Too long."}],
        )
    return value


def create(db: Session, *, name: str, email: str | None, subject: str, body: str, user: User | None = None) -> ContactMessage:
    name_clean = _clean(name, "name", required=user is None) or (user.label if user else "")
    email_clean = _clean(email, "email", required=False)
    if email_clean and not mail_service.is_valid_address(email_clean):
        raise ValidationFailed("Enter a valid email address or leave it blank.", details=[{"field": "email", "message": "Invalid email."}])
    message = ContactMessage(
        user_id=user.id if user else None,
        name=name_clean,
        email=email_clean,
        subject=_clean(subject, "subject", required=True),
        body=_clean(body, "body", required=True, multiline=True),
        status=MESSAGE_NEW,
        created_at=utcnow(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def unread_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ContactMessage).where(ContactMessage.status == MESSAGE_NEW))


def list_for_admin(db: Session, *, status: str = "", q: str = "", page: int = 1, page_size: int = 20):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    conditions = []
    if status == "open":
        conditions.append(ContactMessage.status != MESSAGE_ARCHIVED)
    elif status in (MESSAGE_NEW, MESSAGE_READ, MESSAGE_ARCHIVED):
        conditions.append(ContactMessage.status == status)
    if q.strip():
        like = f"%{q.strip()}%"
        conditions.append(or_(ContactMessage.subject.like(like), ContactMessage.name.like(like), ContactMessage.body.like(like)))
    total = db.scalar(select(func.count()).select_from(ContactMessage).where(*conditions))
    rows = db.scalars(
        select(ContactMessage)
        .where(*conditions)
        .options(selectinload(ContactMessage.user), selectinload(ContactMessage.replied_by))
        .order_by(ContactMessage.created_at.desc(), ContactMessage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return rows, total, page, page_size, max(1, math.ceil(total / page_size))


def list_for_user(db: Session, user_id: int, limit: int = 50):
    return db.scalars(
        select(ContactMessage)
        .where(ContactMessage.user_id == user_id)
        .order_by(ContactMessage.created_at.desc(), ContactMessage.id.desc())
        .limit(limit)
    ).all()


def update(db: Session, *, admin: User, message_id: int, status: str | None = None, reply: str | None = None) -> ContactMessage:
    message = db.get(ContactMessage, message_id)
    if message is None:
        raise NotFound("Message not found.")
    if reply is not None:
        text = _clean(reply, "reply", required=True, multiline=True)
        message.reply = text
        message.replied_at = utcnow()
        message.replied_by_id = admin.id
        message.reply_seen_at = None  # new or edited reply: show it as unread
        if message.status == MESSAGE_NEW:
            message.status = MESSAGE_READ
        audit_service.record(
            db, action="message.replied", actor_id=admin.id, target_user_id=message.user_id,
            details=f"Message #{message.id}: {message.subject[:80]}",
        )
    if status is not None:
        if status not in (MESSAGE_NEW, MESSAGE_READ, MESSAGE_ARCHIVED):
            raise ValidationFailed("Unknown message status.")
        message.status = status
    db.commit()
    db.refresh(message)
    return message


def unseen_replies_count(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(ContactMessage)
        .where(ContactMessage.user_id == user_id, ContactMessage.reply.is_not(None), ContactMessage.reply_seen_at.is_(None))
    ) or 0


def mark_replies_seen(db: Session, user_id: int) -> None:
    db.execute(
        sql_update(ContactMessage)
        .where(ContactMessage.user_id == user_id, ContactMessage.reply.is_not(None), ContactMessage.reply_seen_at.is_(None))
        .values(reply_seen_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.commit()
