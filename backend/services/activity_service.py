"""Read access to the append-only balance activity ledger."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from backend.models import BalanceActivity, User

MAX_PAGE_SIZE = 100


def _filters(change_type: str = "", direction: str = "", date_from: date | None = None, date_to: date | None = None):
    conditions = []
    if change_type in ("set", "adjust"):
        conditions.append(BalanceActivity.change_type == change_type)
    if direction == "credit":
        conditions.append(BalanceActivity.amount_cents > 0)
    elif direction == "debit":
        conditions.append(BalanceActivity.amount_cents < 0)
    if date_from:
        conditions.append(BalanceActivity.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        conditions.append(BalanceActivity.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    return conditions


def _page(db: Session, conditions, page: int, page_size: int, options=()):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    page = max(1, page)
    total = db.scalar(select(func.count()).select_from(BalanceActivity).where(*conditions))
    items = db.scalars(
        select(BalanceActivity)
        .where(*conditions)
        .options(*options)
        .order_by(BalanceActivity.created_at.desc(), BalanceActivity.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return items, total, page, page_size, max(1, math.ceil(total / page_size))


def list_for_user(
    db: Session, user_id: int, *, page: int = 1, page_size: int = 20, with_admin: bool = False,
    change_type: str = "", direction: str = "",
):
    """Newest-first page of one account's activity."""
    conditions = [BalanceActivity.user_id == user_id, *_filters(change_type, direction)]
    options = (selectinload(BalanceActivity.claim),)
    if with_admin:
        options += (selectinload(BalanceActivity.admin), selectinload(BalanceActivity.approved_by))
    return _page(db, conditions, page, page_size, options)


def export_for_user(db: Session, user_id: int):
    """All of one account's activity, newest first (account-holder CSV download)."""
    return db.scalars(
        select(BalanceActivity)
        .where(BalanceActivity.user_id == user_id)
        .options(selectinload(BalanceActivity.claim))
        .order_by(BalanceActivity.created_at.desc(), BalanceActivity.id.desc())
    ).all()


def _global_conditions(q: str, change_type: str, direction: str, date_from, date_to):
    conditions = _filters(change_type, direction, date_from, date_to)
    term = q.strip().lower().lstrip("#")
    if term:
        matching = select(User.id).where(User.username.like(f"%{term}%"))
        options = [BalanceActivity.user_id.in_(matching), BalanceActivity.reason.like(f"%{term}%")]
        if term.isdigit() and len(term) <= 18:
            options.append(BalanceActivity.user_id == int(term))
        conditions.append(or_(*options))
    return conditions


def list_all(
    db: Session, *, q: str = "", change_type: str = "", direction: str = "",
    date_from: date | None = None, date_to: date | None = None, page: int = 1, page_size: int = 25,
):
    conditions = _global_conditions(q, change_type, direction, date_from, date_to)
    options = (selectinload(BalanceActivity.admin), selectinload(BalanceActivity.user),
               selectinload(BalanceActivity.approved_by), selectinload(BalanceActivity.claim))
    return _page(db, conditions, page, page_size, options)


def export_all(db: Session, *, q: str = "", change_type: str = "", direction: str = "", date_from=None, date_to=None, user_id: int | None = None):
    conditions = _global_conditions(q, change_type, direction, date_from, date_to)
    if user_id is not None:
        conditions.append(BalanceActivity.user_id == user_id)
    return db.scalars(
        select(BalanceActivity)
        .where(*conditions)
        .options(selectinload(BalanceActivity.admin), selectinload(BalanceActivity.user),
                 selectinload(BalanceActivity.approved_by))
        .order_by(BalanceActivity.created_at.desc(), BalanceActivity.id.desc())
    ).all()
