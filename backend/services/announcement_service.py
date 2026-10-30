"""Announcements written by administrators and shown on account-holder dashboards."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.errors import NotFound, ValidationFailed
from backend.models import Announcement, User, utcnow
from backend.services import audit_service

LEVELS = ("info", "success", "warning")


def _validate(title: str, body: str, level: str) -> tuple[str, str, str]:
    title = " ".join((title or "").split())
    body = (body or "").strip()
    if not title or len(title) > 120:
        raise ValidationFailed("Title is required (max 120 characters).", details=[{"field": "title", "message": "Invalid title."}])
    if not body or len(body) > 2000:
        raise ValidationFailed("Message is required (max 2000 characters).", details=[{"field": "body", "message": "Invalid message."}])
    if level not in LEVELS:
        raise ValidationFailed("Style must be info, success or warning.")
    return title, body, level


def list_published(db: Session, limit: int = 5):
    return db.scalars(
        select(Announcement)
        .where(Announcement.published.is_(True))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .limit(limit)
    ).all()


def list_all(db: Session):
    return db.scalars(
        select(Announcement)
        .options(selectinload(Announcement.author))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
    ).all()


def create(db: Session, *, admin: User, title: str, body: str, level: str = "info", published: bool = True, pinned: bool = False) -> Announcement:
    title, body, level = _validate(title, body, level)
    now = utcnow()
    item = Announcement(
        title=title, body=body, level=level, published=published, pinned=pinned,
        author_id=admin.id, created_at=now, updated_at=now,
    )
    db.add(item)
    db.flush()
    audit_service.record(db, action="announcement.created", actor_id=admin.id, details=f"#{item.id} {title}")
    db.commit()
    db.refresh(item)
    return item


def update(db: Session, *, admin: User, announcement_id: int, **fields) -> Announcement:
    item = db.get(Announcement, announcement_id)
    if item is None:
        raise NotFound("Announcement not found.")
    title, body, level = _validate(
        fields.get("title", item.title), fields.get("body", item.body), fields.get("level", item.level)
    )
    item.title, item.body, item.level = title, body, level
    if fields.get("published") is not None:
        item.published = fields["published"]
    if fields.get("pinned") is not None:
        item.pinned = fields["pinned"]
    item.updated_at = utcnow()
    audit_service.record(db, action="announcement.updated", actor_id=admin.id, details=f"#{item.id} {item.title}")
    db.commit()
    db.refresh(item)
    return item


def delete(db: Session, *, admin: User, announcement_id: int) -> None:
    item = db.get(Announcement, announcement_id)
    if item is None:
        raise NotFound("Announcement not found.")
    audit_service.record(db, action="announcement.deleted", actor_id=admin.id, details=f"#{item.id} {item.title}")
    db.delete(item)
    db.commit()
