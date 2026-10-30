"""Administrator pages and API."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.csv_export import cents_str as _cents_str
from backend.csv_export import csv_response as _csv_response
from backend.csv_export import csv_text as _csv_text
from backend.csv_export import iso as _iso
from backend.database import get_db
from backend.dependencies import get_settings, page_admin, require_admin, require_admin_write
from backend.errors import NotFound, ValidationFailed
from backend.models import User
from backend.money import MAX_AMOUNT_CENTS, format_cents
from backend.schemas import (
    AdminActivityOut,
    AdminActivityPage,
    AnnouncementOut,
    AnnouncementRequest,
    ApprovalDecisionRequest,
    ApprovalListOut,
    AuditOut,
    AuditPage,
    BalanceRequest,
    BalanceResponse,
    BulkRequest,
    ClaimAdminUpdateRequest,
    ClaimAssigneeOut,
    ClaimCreditRequest,
    ClaimMetricsOut,
    CreateUserRequest,
    CreateUserResponse,
    MessageOut,
    MessagePage,
    MessageUpdateRequest,
    NoteOut,
    NoteRequest,
    OkResponse,
    PendingChangeOut,
    RefundClaimOut,
    RefundClaimPage,
    SavedReplyOut,
    SavedReplyRequest,
    SettingsOut,
    SettingsRequest,
    StatsResponse,
    StatusRequest,
    TotpDisableRequest,
    TotpEnableRequest,
    UserDetailResponse,
    UserPage,
    UserSummary,
)
from backend.security import TOTP_SETUP_SESSION_KEY, client_ip, generate_totp_secret, totp_uri
from backend.services import (
    account_service,
    activity_service,
    announcement_service,
    approval_service,
    audit_service,
    claim_service,
    message_service,
    saved_reply_service,
    settings_service,
)
from backend.templating import render

router = APIRouter()

UserId = Path(..., ge=1, le=2**62)
Page_ = Query(1, ge=1, le=1_000_000)
Sort = Query("newest", pattern="^(newest|oldest|balance_desc|balance_asc|username|last_login)$")
ClaimQueue = Query("", pattern="^(|mine|unassigned|overdue|stale)$")


# --- pages ---------------------------------------------------------------------


def _admin_page(request: Request, admin: User, db: Session, settings: Settings, template: str, active: str, **extra):
    return render(request, template, {
        "current_user": admin,
        "active_nav": active,
        "unread_messages": message_service.unread_count(db),
        "unread_claims": claim_service.open_count(db),
        "pending_approvals": approval_service.pending_count(db),
        "allow_negative": settings.allow_negative,
        "max_amount_cents": MAX_AMOUNT_CENTS,
        **extra,
    })


def _simple_page(template: str, active: str):
    def view(request: Request, admin: User = Depends(page_admin), settings: Settings = Depends(get_settings),
             db: Session = Depends(get_db)):
        return _admin_page(request, admin, db, settings, template, active)
    return view


router.add_api_route("/admin", _simple_page("admin/overview.html", "overview"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/accounts", _simple_page("admin.html", "accounts"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/activity", _simple_page("admin/activity.html", "activity"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/claims", _simple_page("admin/claims.html", "claims"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/messages", _simple_page("admin/messages.html", "messages"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/announcements", _simple_page("admin/announcements.html", "announcements"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/audit", _simple_page("admin/audit.html", "audit"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/settings", _simple_page("admin/settings.html", "settings"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/approvals", _simple_page("admin/approvals.html", "approvals"), methods=["GET"], include_in_schema=False)
router.add_api_route("/admin/security", _simple_page("admin/security.html", "security"), methods=["GET"], include_in_schema=False)


@router.get("/admin/users/{user_id}", include_in_schema=False)
def admin_user_page(
    request: Request,
    user_id: int = UserId,
    admin: User = Depends(page_admin),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if account is None:
        raise NotFound("Account not found.")
    return _admin_page(request, admin, db, settings, "admin_user_detail.html", "accounts", account=account)


# --- helpers -------------------------------------------------------------------


def _activity_page(db: Session, user_id: int, page: int, page_size: int) -> AdminActivityPage:
    items, total, page, page_size, pages = activity_service.list_for_user(
        db, user_id, page=page, page_size=page_size, with_admin=True
    )
    return AdminActivityPage(
        items=[AdminActivityOut.from_activity(a) for a in items], page=page, page_size=page_size, total=total, pages=pages
    )


def _claim_out(db: Session, claim, *, with_duplicates: bool = False) -> RefundClaimOut:
    extra = claim_service.annotate(db, [claim], for_admin=True, with_duplicates=with_duplicates)[claim.id]
    return RefundClaimOut.from_claim(claim, include_internal=True, extra=extra)


# --- overview & statistics ------------------------------------------------------


@router.get("/api/admin/stats/", response_model=StatsResponse, tags=["admin"])
def stats(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    data = account_service.admin_stats(db)
    return StatsResponse(**data, total_balance_display=format_cents(data["total_balance_cents"]))


@router.get("/api/admin/overview/", tags=["admin"])
def overview(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    data = account_service.overview(db)
    return {
        "recent_signups": [UserSummary.from_user(u).model_dump(mode="json") for u in data["recent_signups"]],
        "top_balances": [UserSummary.from_user(u).model_dump(mode="json") for u in data["top_balances"]],
        "recent_activity": [AdminActivityOut.from_activity(a).model_dump(mode="json") for a in data["recent_activity"]],
    }


@router.get("/api/admin/charts/", tags=["admin"])
def charts(days: int = Query(30, ge=7, le=90), _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return account_service.chart_series(db, days)


# --- accounts -------------------------------------------------------------------


@router.get("/api/admin/users/", response_model=UserPage, tags=["admin"])
def list_users(
    q: str = Query("", max_length=64),
    status: str = Query("", pattern="^(|active|inactive)$"),
    role: str = Query("", pattern="^(|user|admin)$"),
    sort: str = Sort,
    page: int = Page_,
    page_size: int = Query(20, ge=1, le=account_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, total, page, page_size, pages = account_service.list_accounts(
        db, q=q, status=status, role=role, sort=sort, page=page, page_size=page_size
    )
    return UserPage(items=[UserSummary.from_user(u) for u in rows], page=page, page_size=page_size, total=total, pages=pages)


@router.get("/api/admin/users/export.csv", tags=["admin"])
def export_users(
    q: str = Query("", max_length=64),
    status: str = Query("", pattern="^(|active|inactive)$"),
    role: str = Query("", pattern="^(|user|admin)$"),
    sort: str = Sort,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = account_service.export_accounts(db, q=q, status=status, role=role, sort=sort)
    return _csv_response(
        "accounts.csv",
        ["id", "username", "display_name", "role", "status", "balance_usd", "signup_source", "created_at_utc", "last_login_utc"],
        ([u.id, _csv_text(u.username), _csv_text(u.display_name), u.role, u.status, _cents_str(u.balance_cents),
          u.signup_source or "", _iso(u.created_at), _iso(u.last_login_at)] for u in rows),
    )


@router.post("/api/admin/users/", status_code=201, response_model=CreateUserResponse, tags=["admin"])
def create_user(payload: CreateUserRequest, request: Request, admin: User = Depends(require_admin_write),
                db: Session = Depends(get_db)):
    created = account_service.create_account(
        db, username=payload.username, password=payload.password, generate_password=payload.generate_password,
        display_name=payload.display_name, actor=admin, ip=client_ip(request),
    )
    return CreateUserResponse(user=UserSummary.from_user(created.user), temporary_password=created.temporary_password)


@router.post("/api/admin/users/bulk/", tags=["admin"])
def bulk_action(payload: BulkRequest, admin: User = Depends(require_admin_write), settings: Settings = Depends(get_settings),
                db: Session = Depends(get_db)):
    ids = list(dict.fromkeys(payload.user_ids))
    if payload.action == "adjust":
        if not payload.amount_cents:
            raise ValidationFailed("Enter a non-zero adjustment amount.", details=[{"field": "amount_cents", "message": "Required."}])
        results = approval_service.bulk_adjust(
            db, admin=admin, user_ids=ids, amount_cents=payload.amount_cents, reason=payload.reason,
            allow_negative=settings.allow_negative,
        )
    else:
        status = "active" if payload.action == "activate" else "inactive"
        results = account_service.bulk_set_status(db, actor=admin, user_ids=ids, status=status)
    succeeded = sum(1 for r in results if r["ok"])
    return {"succeeded": succeeded, "failed": len(results) - succeeded, "results": results}


@router.get("/api/admin/users/{user_id}/", response_model=UserDetailResponse, tags=["admin"])
def user_detail(
    user_id: int = UserId,
    page: int = Page_,
    page_size: int = Query(20, ge=1, le=activity_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = account_service.get_user(db, user_id)
    return UserDetailResponse(user=UserSummary.from_user(user), activity=_activity_page(db, user.id, page, page_size))


@router.get("/api/admin/users/{user_id}/activity/", response_model=AdminActivityPage, tags=["admin"])
def user_activity(
    user_id: int = UserId,
    page: int = Page_,
    page_size: int = Query(20, ge=1, le=activity_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = account_service.get_user(db, user_id)
    return _activity_page(db, user.id, page, page_size)


@router.post("/api/admin/users/{user_id}/status/", response_model=UserSummary, tags=["admin"])
def change_status(payload: StatusRequest, user_id: int = UserId, admin: User = Depends(require_admin_write),
                  db: Session = Depends(get_db)):
    return UserSummary.from_user(account_service.set_status(db, actor=admin, user_id=user_id, status=payload.status))


@router.post("/api/admin/users/{user_id}/password-reset/", tags=["admin"])
def reset_password(user_id: int = UserId, admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    user, temporary = account_service.reset_password(db, admin=admin, user_id=user_id)
    return {"user": UserSummary.from_user(user).model_dump(mode="json"), "temporary_password": temporary}


@router.get("/api/admin/users/{user_id}/notes/", response_model=list[NoteOut], tags=["admin"])
def list_notes(user_id: int = UserId, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [NoteOut.from_note(n) for n in account_service.list_notes(db, user_id)]


@router.post("/api/admin/users/{user_id}/notes/", status_code=201, response_model=NoteOut, tags=["admin"])
def add_note(payload: NoteRequest, user_id: int = UserId, admin: User = Depends(require_admin_write),
             db: Session = Depends(get_db)):
    return NoteOut.from_note(account_service.add_note(db, admin=admin, user_id=user_id, body=payload.body))


@router.delete("/api/admin/users/{user_id}/notes/{note_id}/", response_model=OkResponse, tags=["admin"])
def delete_note(user_id: int = UserId, note_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write),
                db: Session = Depends(get_db)):
    account_service.delete_note(db, admin=admin, user_id=user_id, note_id=note_id)
    return OkResponse()


@router.post("/api/admin/users/{user_id}/balance/", response_model=BalanceResponse, tags=["admin"])
def change_balance(
    payload: Annotated[BalanceRequest, Body()],
    user_id: int = UserId,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    admin: User = Depends(require_admin_write),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    value = payload.target_cents if payload.mode == "set" else payload.amount_cents
    outcome = approval_service.submit_change(
        db,
        admin=admin,
        user_id=user_id,
        mode=payload.mode,
        value=value,
        reason=payload.reason,
        allow_negative=settings.allow_negative,
        idempotency_key=idempotency_key,
    )
    if outcome.pending is not None:
        # At or above the approval threshold: queued for a second administrator.
        pending = approval_service.get_request(db, outcome.pending.id)
        return JSONResponse({"pending": PendingChangeOut.from_pending(pending).model_dump(mode="json")}, status_code=202)
    result = outcome.result
    activity = result.activity
    body = BalanceResponse(
        user=UserSummary.from_user(result.user),
        activity=AdminActivityOut.from_activity(activity),
        previous_cents=activity.previous_cents,
        amount_cents=activity.amount_cents,
        resulting_cents=activity.resulting_cents,
        replayed=result.replayed,
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=200 if result.replayed else 201)


# --- global activity ledger -----------------------------------------------------------


@router.get("/api/admin/activity/", response_model=AdminActivityPage, tags=["admin"])
def all_activity(
    q: str = Query("", max_length=64),
    type: str = Query("", pattern="^(|set|adjust)$"),
    direction: str = Query("", pattern="^(|credit|debit)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    page: int = Page_,
    page_size: int = Query(25, ge=1, le=activity_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    items, total, page, page_size, pages = activity_service.list_all(
        db, q=q, change_type=type, direction=direction, date_from=date_from, date_to=date_to, page=page, page_size=page_size
    )
    return AdminActivityPage(
        items=[AdminActivityOut.from_activity(a) for a in items], page=page, page_size=page_size, total=total, pages=pages
    )


@router.get("/api/admin/activity/export.csv", tags=["admin"])
def export_activity(
    q: str = Query("", max_length=64),
    type: str = Query("", pattern="^(|set|adjust)$"),
    direction: str = Query("", pattern="^(|credit|debit)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    user_id: int | None = Query(None, ge=1, le=2**62),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = activity_service.export_all(
        db, q=q, change_type=type, direction=direction, date_from=date_from, date_to=date_to, user_id=user_id
    )
    return _csv_response(
        "activity.csv" if user_id is None else f"activity-account-{user_id}.csv",
        ["id", "created_at_utc", "account_id", "username", "type", "previous_usd", "change_usd", "resulting_usd", "reason",
         "recorded_by", "approved_by", "claim_id"],
        ([a.id, _iso(a.created_at), a.user_id, _csv_text(a.user.username if a.user else ""), a.change_type,
          _cents_str(a.previous_cents), _cents_str(a.amount_cents), _cents_str(a.resulting_cents),
          _csv_text(a.reason), _csv_text(a.admin.username if a.admin else ""),
          _csv_text(a.approved_by.username if a.approved_by else ""), a.claim_id or ""] for a in rows),
    )


# --- messages ---------------------------------------------------------------------------


@router.get("/api/admin/claims/", response_model=RefundClaimPage, tags=["admin"])
def list_claims(
    status: str = Query("", pattern="^(|submitted|under_review|awaiting_customer|approved|declined|cancelled)$"),
    priority: str = Query("", pattern="^(|normal|high|urgent)$"),
    q: str = Query("", max_length=100),
    queue: str = ClaimQueue,
    page: int = Page_,
    page_size: int = Query(20, ge=1, le=claim_service.MAX_PAGE_SIZE),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, total, page, page_size, pages = claim_service.list_for_admin(
        db, status=status, priority=priority, q=q, queue=queue, admin=admin, page=page, page_size=page_size
    )
    extras = claim_service.annotate(db, rows, for_admin=True)
    return RefundClaimPage(
        items=[RefundClaimOut.from_claim(claim, include_internal=True, extra=extras[claim.id]) for claim in rows],
        page=page, page_size=page_size, total=total, pages=pages,
    )


@router.get("/api/admin/claims/export.csv", tags=["admin"])
def export_claims(
    status: str = Query("", pattern="^(|submitted|under_review|awaiting_customer|approved|declined|cancelled)$"),
    priority: str = Query("", pattern="^(|normal|high|urgent)$"),
    q: str = Query("", max_length=100),
    queue: str = ClaimQueue,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, _, page, page_size, pages = claim_service.list_for_admin(
        db, status=status, priority=priority, q=q, queue=queue, admin=admin, page=1, page_size=claim_service.MAX_PAGE_SIZE
    )
    for next_page in range(page + 1, pages + 1):
        next_rows, _, _, _, _ = claim_service.list_for_admin(
            db, status=status, priority=priority, q=q, queue=queue, admin=admin, page=next_page, page_size=page_size
        )
        rows.extend(next_rows)
    extras = claim_service.annotate(db, rows, for_admin=True)
    return _csv_response(
        "refund-claims.csv",
        ["claim_id", "account_id", "username", "merchant", "order_reference", "purchase_date", "amount_usd", "reason", "status", "priority", "assigned_admin", "description", "created_at_utc", "updated_at_utc",
         "status_since_utc", "credited_usd", "possible_duplicate"],
        ([c.id, c.user_id, _csv_text(c.user.username if c.user else ""), _csv_text(c.merchant),
          _csv_text(c.order_reference), c.purchase_date.isoformat(), _cents_str(c.amount_cents), c.reason,
          c.status, c.priority, _csv_text(c.assigned_admin.username if c.assigned_admin else ""),
          _csv_text(c.description), _iso(c.created_at), _iso(c.updated_at), _iso(extras[c.id]["status_since"]),
          _cents_str(extras[c.id]["credit_cents"]) if extras[c.id]["credit_cents"] is not None else "",
          "yes" if extras[c.id]["possible_duplicate"] else ""] for c in rows),
    )


@router.get("/api/admin/claims/metrics/", response_model=ClaimMetricsOut, tags=["admin"])
def claim_metrics(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return ClaimMetricsOut(**claim_service.metrics(db))


@router.get("/api/admin/claims/assignees/", response_model=list[ClaimAssigneeOut], tags=["admin"])
def claim_assignees(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [ClaimAssigneeOut(id=admin.id, username=admin.username, display_name=admin.display_name)
            for admin in claim_service.list_assignees(db)]


@router.get("/api/admin/claims/queues/", tags=["admin"])
def claim_queue_counts(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return {**claim_service.queue_counts(db, admin), "stale_after_days": settings_service.stale_claim_days(db)}


@router.post("/api/admin/claims/close-stale/", tags=["admin"])
def close_stale_claims(admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    return {"closed": claim_service.close_stale(db, admin=admin)}


@router.get("/api/admin/claims/{claim_id}/", response_model=RefundClaimOut, tags=["admin"])
def get_claim(claim_id: int = Path(..., ge=1, le=2**62), _: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    return _claim_out(db, claim_service.get_for_admin(db, claim_id), with_duplicates=True)


@router.get("/api/admin/claims/{claim_id}/evidence/{evidence_id}/", tags=["admin"])
def download_admin_claim_evidence(
    request: Request,
    claim_id: int = Path(..., ge=1, le=2**62),
    evidence_id: int = Path(..., ge=1, le=2**62),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    evidence = claim_service.get_evidence_for_admin(db, claim_id=claim_id, evidence_id=evidence_id)
    path = claim_service.evidence_path(evidence, upload_dir=request.app.state.settings.claim_upload_dir)
    return FileResponse(path, filename=evidence.original_filename, media_type=evidence.content_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/api/admin/claims/{claim_id}/", response_model=RefundClaimOut, tags=["admin"])
def update_claim(payload: ClaimAdminUpdateRequest, claim_id: int = Path(..., ge=1, le=2**62),
                 admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    claim = claim_service.update_by_admin(db, claim_id=claim_id, admin=admin, **payload.model_dump())
    return _claim_out(db, claim, with_duplicates=True)


@router.post("/api/admin/claims/{claim_id}/credit/", tags=["admin"])
def credit_claim(payload: ClaimCreditRequest, claim_id: int = Path(..., ge=1, le=2**62),
                 admin: User = Depends(require_admin_write), settings: Settings = Depends(get_settings),
                 db: Session = Depends(get_db)):
    """Approve a claim and credit the account in one step (or queue the credit for approval)."""
    claim, pending = claim_service.approve_with_credit(
        db, claim_id=claim_id, admin=admin, credit_cents=payload.credit_cents, public_reply=payload.public_reply,
        allow_negative=settings.allow_negative,
    )
    body = {"claim": _claim_out(db, claim, with_duplicates=True).model_dump(mode="json"), "pending": None}
    if pending is not None:
        body["pending"] = PendingChangeOut.from_pending(approval_service.get_request(db, pending.id)).model_dump(mode="json")
    return JSONResponse(body, status_code=202 if pending is not None else 201)


@router.get("/api/admin/messages/", response_model=MessagePage, tags=["admin"])
def list_messages(
    status: str = Query("open", pattern="^(|open|new|read|archived)$"),
    q: str = Query("", max_length=64),
    page: int = Page_,
    page_size: int = Query(20, ge=1, le=message_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, total, page, page_size, pages = message_service.list_for_admin(db, status=status, q=q, page=page, page_size=page_size)
    return MessagePage(items=[MessageOut.from_message(m) for m in rows], page=page, page_size=page_size, total=total, pages=pages)


@router.post("/api/admin/messages/{message_id}/", response_model=MessageOut, tags=["admin"])
def update_message(payload: MessageUpdateRequest, message_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write),
                   db: Session = Depends(get_db)):
    message = message_service.update(db, admin=admin, message_id=message_id, status=payload.status, reply=payload.reply)
    return MessageOut.from_message(message)


# --- announcements ---------------------------------------------------------------------------


@router.get("/api/admin/announcements/", response_model=list[AnnouncementOut], tags=["admin"])
def list_announcements(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [AnnouncementOut.model_validate(a) for a in announcement_service.list_all(db)]


@router.post("/api/admin/announcements/", status_code=201, response_model=AnnouncementOut, tags=["admin"])
def create_announcement(payload: AnnouncementRequest, admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    return AnnouncementOut.model_validate(announcement_service.create(db, admin=admin, **payload.model_dump()))


@router.post("/api/admin/announcements/{announcement_id}/", response_model=AnnouncementOut, tags=["admin"])
def update_announcement(payload: AnnouncementRequest, announcement_id: int = Path(..., ge=1, le=2**62),
                        admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    item = announcement_service.update(db, admin=admin, announcement_id=announcement_id, **payload.model_dump())
    return AnnouncementOut.model_validate(item)


@router.delete("/api/admin/announcements/{announcement_id}/", response_model=OkResponse, tags=["admin"])
def delete_announcement(announcement_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write),
                        db: Session = Depends(get_db)):
    announcement_service.delete(db, admin=admin, announcement_id=announcement_id)
    return OkResponse()


# --- audit log & settings ----------------------------------------------------------------------


@router.get("/api/admin/audit/", response_model=AuditPage, tags=["admin"])
def audit_log(
    action: str = Query("", max_length=40, pattern=r"^[a-z_.]*$"),
    q: str = Query("", max_length=64),
    page: int = Page_,
    page_size: int = Query(25, ge=1, le=audit_service.MAX_PAGE_SIZE),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, total, page, page_size, pages = audit_service.list_entries(db, action=action, q=q, page=page, page_size=page_size)
    return AuditPage(
        items=[AuditOut.from_entry(e, audit_service.ACTION_LABELS) for e in rows],
        page=page, page_size=page_size, total=total, pages=pages,
    )


@router.get("/api/admin/settings/", response_model=SettingsOut, tags=["admin"])
def get_app_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return SettingsOut(**settings_service.get_all(db))


@router.post("/api/admin/settings/", response_model=SettingsOut, tags=["admin"])
def update_app_settings(payload: SettingsRequest, admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    values = settings_service.update(db, actor=admin, **payload.model_dump())
    return SettingsOut(**values)


# --- saved replies ------------------------------------------------------------------------------


@router.get("/api/admin/saved-replies/", response_model=list[SavedReplyOut], tags=["admin"])
def list_saved_replies(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [SavedReplyOut.model_validate(r) for r in saved_reply_service.list_all(db)]


@router.post("/api/admin/saved-replies/", status_code=201, response_model=SavedReplyOut, tags=["admin"])
def create_saved_reply(payload: SavedReplyRequest, admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    return SavedReplyOut.model_validate(saved_reply_service.create(db, admin=admin, title=payload.title, body=payload.body))


@router.post("/api/admin/saved-replies/{reply_id}/", response_model=SavedReplyOut, tags=["admin"])
def update_saved_reply(payload: SavedReplyRequest, reply_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write),
                       db: Session = Depends(get_db)):
    item = saved_reply_service.update(db, admin=admin, reply_id=reply_id, title=payload.title, body=payload.body)
    return SavedReplyOut.model_validate(item)


@router.delete("/api/admin/saved-replies/{reply_id}/", response_model=OkResponse, tags=["admin"])
def delete_saved_reply(reply_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    saved_reply_service.delete(db, admin=admin, reply_id=reply_id)
    return OkResponse()


# --- second-administrator approvals -------------------------------------------------------------


@router.get("/api/admin/approvals/", response_model=ApprovalListOut, tags=["admin"])
def list_approvals(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    pending, recent = approval_service.list_requests(db)
    return ApprovalListOut(pending=[PendingChangeOut.from_pending(p) for p in pending],
                           recent=[PendingChangeOut.from_pending(p) for p in recent])


@router.post("/api/admin/approvals/{request_id}/approve/", response_model=PendingChangeOut, tags=["admin"])
def approve_change(request_id: int = Path(..., ge=1, le=2**62), admin: User = Depends(require_admin_write),
                   settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    pending = approval_service.approve(db, approver=admin, pending_id=request_id, allow_negative=settings.allow_negative)
    return PendingChangeOut.from_pending(pending)


@router.post("/api/admin/approvals/{request_id}/reject/", response_model=PendingChangeOut, tags=["admin"])
def reject_change(payload: ApprovalDecisionRequest, request_id: int = Path(..., ge=1, le=2**62),
                  admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    return PendingChangeOut.from_pending(approval_service.reject(db, admin=admin, pending_id=request_id, note=payload.note))


# --- administrator two-step sign-in -------------------------------------------------------------


@router.get("/api/admin/security/", tags=["admin"])
def security_status(admin: User = Depends(require_admin)):
    return {"totp_enabled": bool(admin.totp_secret)}


@router.post("/api/admin/security/totp/setup/", tags=["admin"])
def totp_setup(request: Request, admin: User = Depends(require_admin_write)):
    """Start enrolment: a fresh secret kept in this session until a code confirms it."""
    if admin.totp_secret:
        raise ValidationFailed("Two-step sign-in is already on.")
    secret = generate_totp_secret()
    request.session[TOTP_SETUP_SESSION_KEY] = secret
    return {"secret": secret, "otpauth_uri": totp_uri(secret, admin.username, request.app.state.settings.site_name)}


@router.post("/api/admin/security/totp/enable/", tags=["admin"])
def totp_enable(payload: TotpEnableRequest, request: Request, admin: User = Depends(require_admin_write),
                db: Session = Depends(get_db)):
    secret = request.session.get(TOTP_SETUP_SESSION_KEY)
    if not secret:
        raise ValidationFailed("Start the setup again; the setup key expired.")
    account_service.enable_totp(db, user=admin, secret=secret, code=payload.code, ip=client_ip(request))
    request.session.pop(TOTP_SETUP_SESSION_KEY, None)
    return {"totp_enabled": True}


@router.post("/api/admin/security/totp/disable/", tags=["admin"])
def totp_disable(payload: TotpDisableRequest, request: Request, admin: User = Depends(require_admin_write),
                 db: Session = Depends(get_db)):
    account_service.disable_totp(db, user=admin, password=payload.password, code=payload.code, ip=client_ip(request))
    return {"totp_enabled": False}


@router.post("/api/admin/users/{user_id}/totp-reset/", response_model=UserSummary, tags=["admin"])
def reset_user_totp(user_id: int = UserId, admin: User = Depends(require_admin_write), db: Session = Depends(get_db)):
    return UserSummary.from_user(account_service.reset_totp(db, admin=admin, user_id=user_id))
