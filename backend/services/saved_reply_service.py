"""Saved replies: reusable text for claim updates and support messages."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.errors import NotFound, ValidationFailed
from backend.models import SavedReply, User, utcnow
from backend.services import audit_service

TITLE_MAX = 80
BODY_MAX = 4000
MAX_SAVED_REPLIES = 100


def _clean(title: str, body: str) -> tuple[str, str]:
    title = " ".join((title or "").split())
    body = (body or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise ValidationFailed(f"Title must be 1-{TITLE_MAX} characters.", details=[{"field": "title", "message": "Invalid title."}])
    if not body or len(body) > BODY_MAX:
        raise ValidationFailed(f"Reply text must be 1-{BODY_MAX} characters.", details=[{"field": "body", "message": "Invalid text."}])
    return title, body


def list_all(db: Session) -> list[SavedReply]:
    return db.scalars(select(SavedReply).order_by(SavedReply.title.asc(), SavedReply.id.asc())).all()


def create(db: Session, *, admin: User, title: str, body: str) -> SavedReply:
    title, body = _clean(title, body)
    if len(list_all(db)) >= MAX_SAVED_REPLIES:
        raise ValidationFailed(f"You can keep at most {MAX_SAVED_REPLIES} saved replies.")
    item = SavedReply(title=title, body=body, author_id=admin.id, created_at=utcnow(), updated_at=utcnow())
    db.add(item)
    audit_service.record(db, action="saved_reply.created", actor_id=admin.id, details=title)
    db.commit()
    db.refresh(item)
    return item


def update(db: Session, *, admin: User, reply_id: int, title: str, body: str) -> SavedReply:
    item = db.get(SavedReply, reply_id)
    if item is None:
        raise NotFound("Saved reply not found.")
    item.title, item.body = _clean(title, body)
    item.updated_at = utcnow()
    audit_service.record(db, action="saved_reply.updated", actor_id=admin.id, details=item.title)
    db.commit()
    db.refresh(item)
    return item


def delete(db: Session, *, admin: User, reply_id: int) -> None:
    item = db.get(SavedReply, reply_id)
    if item is None:
        raise NotFound("Saved reply not found.")
    db.delete(item)
    audit_service.record(db, action="saved_reply.deleted", actor_id=admin.id, details=item.title)
    db.commit()
