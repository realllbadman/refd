"""Account-holder area: overview, activity, support messages, profile."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.csv_export import cents_str, csv_response, csv_text, iso
from backend.database import get_db
from backend.dependencies import get_settings, page_user, require_customer, verify_csrf
from backend.errors import ValidationFailed
from backend.models import User
from backend.money import format_cents
from backend.schemas import (
    ActivityOut,
    ActivityPage,
    AnnouncementOut,
    ClaimCreateRequest,
    ClaimCustomerMessageRequest,
    ClaimUserUpdateRequest,
    OkResponse,
    PasswordChangeRequest,
    PasswordConfirmRequest,
    ProfileRequest,
    RefundClaimOut,
    SignInOut,
    SupportMessageRequest,
    UserMessageOut,
    UserSummary,
    UserSummaryStats,
)
from backend.security import client_ip, end_session, start_session
from backend.services import (
    account_service,
    activity_service,
    announcement_service,
    claim_service,
    mail_service,
    message_service,
)
from backend.templating import render

router = APIRouter()


def _user_page(request: Request, user: User, template: str, active: str, db: Session, **extra):
    if user.is_admin:
        return RedirectResponse("/admin", status_code=303)
    return render(request, template, {
        "current_user": user, "account": user, "active_nav": active,
        "claim_updates": claim_service.updates_count_for_user(db, user.id),
        "reply_updates": message_service.unseen_replies_count(db, user.id),
        **extra,
    })


def _claims_out(db: Session, claims) -> list[RefundClaimOut]:
    extras = claim_service.annotate(db, claims, for_admin=False)
    return [RefundClaimOut.from_claim(c, extra=extras[c.id]) for c in claims]


def _claim_out(db: Session, claim) -> RefundClaimOut:
    return _claims_out(db, [claim])[0]


@router.get("/dashboard", include_in_schema=False)
def dashboard(request: Request, welcome: int = 0, user: User = Depends(page_user), db: Session = Depends(get_db)):
    return _user_page(request, user, "dashboard.html", "dashboard", db, welcome=bool(welcome),
                      recovery_codes_remaining=account_service.recovery_codes_remaining(db, user.id))


@router.get("/dashboard/activity", include_in_schema=False)
def activity_page(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    return _user_page(request, user, "user/activity.html", "activity", db)


@router.get("/dashboard/support", include_in_schema=False)
def support_page(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    return _user_page(request, user, "user/support.html", "support", db)


@router.get("/dashboard/profile", include_in_schema=False)
def profile_page(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    return _user_page(request, user, "user/profile.html", "profile", db,
                      recovery_codes_remaining=account_service.recovery_codes_remaining(db, user.id))


@router.get("/dashboard/claims", include_in_schema=False)
def claims_page(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    return _user_page(request, user, "user/claims.html", "claims", db)


# --- API -----------------------------------------------------------------------------


@router.get("/api/user/activity/", response_model=ActivityPage, tags=["user"])
def my_activity(
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=activity_service.MAX_PAGE_SIZE),
    type: str = Query("", pattern="^(|set|adjust)$"),
    direction: str = Query("", pattern="^(|credit|debit)$"),
    user: User = Depends(require_customer),
    db: Session = Depends(get_db),
):
    # The account is always taken from the session, never from the request.
    items, total, page, page_size, pages = activity_service.list_for_user(
        db, user.id, page=page, page_size=page_size, change_type=type, direction=direction
    )
    return ActivityPage(
        items=[ActivityOut.from_activity(a) for a in items], page=page, page_size=page_size, total=total, pages=pages
    )


@router.get("/api/user/activity/export.csv", tags=["user"])
def export_my_activity(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    rows = activity_service.export_for_user(db, user.id)
    return csv_response(
        "my-activity.csv",
        ["id", "created_at_utc", "type", "previous_usd", "change_usd", "resulting_usd", "reason", "claim_id"],
        ([a.id, iso(a.created_at), a.change_type, cents_str(a.previous_cents), cents_str(a.amount_cents),
          cents_str(a.resulting_cents), csv_text(a.reason), a.claim_id or ""] for a in rows),
    )


@router.get("/api/user/summary/", response_model=UserSummaryStats, tags=["user"])
def my_summary(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return UserSummaryStats(**account_service.user_summary(db, user))


@router.get("/api/user/announcements/", response_model=list[AnnouncementOut], tags=["user"])
def announcements(_: User = Depends(require_customer), db: Session = Depends(get_db)):
    return [AnnouncementOut.model_validate(a) for a in announcement_service.list_published(db, limit=5)]


@router.get("/api/user/messages/", response_model=list[UserMessageOut], tags=["user"])
def my_messages(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return [UserMessageOut.from_message(m) for m in message_service.list_for_user(db, user.id)]


@router.post("/api/user/messages/seen/", response_model=OkResponse, tags=["user"], dependencies=[Depends(verify_csrf)])
def messages_seen(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    message_service.mark_replies_seen(db, user.id)
    return OkResponse()


@router.get("/api/user/claims/", response_model=list[RefundClaimOut], tags=["user"])
def my_claims(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return _claims_out(db, claim_service.list_for_user(db, user.id))


@router.post("/api/user/claims/seen/", response_model=OkResponse, tags=["user"], dependencies=[Depends(verify_csrf)])
def claims_seen(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    claim_service.mark_seen_for_user(db, user.id)
    return OkResponse()


@router.post("/api/user/claims/", status_code=201, response_model=RefundClaimOut, tags=["user"],
             dependencies=[Depends(verify_csrf)])
def create_claim(payload: ClaimCreateRequest, request: Request, tasks: BackgroundTasks,
                 user: User = Depends(require_customer), settings: Settings = Depends(get_settings),
                 db: Session = Depends(get_db)):
    fields = payload.model_dump()
    contact_email = " ".join((fields.pop("contact_email") or "").split()) or None
    if contact_email and not mail_service.is_valid_address(contact_email):
        raise ValidationFailed("Enter a valid email address or leave it blank.",
                               details=[{"field": "contact_email", "message": "Enter a valid email address or leave it blank."}])
    limiter = request.app.state.claim_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        from backend.errors import RateLimited

        raise RateLimited("You have submitted several claims recently. Please try again later.", limiter.retry_after(ip))
    claim = claim_service.create(db, user=user, **fields)
    limiter.record_failure(ip)
    mail_service.queue_request_emails(
        tasks, settings,
        kind="Refund claim",
        subject=f"Claim #{claim.id} · {claim.merchant}",
        fields=[("Claim ID", f"#{claim.id}"), ("Account", f"@{user.username} (#{user.id})"),
                ("Display name", user.display_name or "Not set"), ("Email", contact_email or "Not given"),
                ("Merchant", claim.merchant), ("Order reference", claim.order_reference),
                ("Purchase date", claim.purchase_date.isoformat()), ("Amount", format_cents(claim.amount_cents)),
                ("Reason", claim_service.REASON_LABELS.get(claim.reason, claim.reason)),
                ("Description", claim.description),
                ("Documents", "Any uploaded files are on the claim in the admin panel.")],
        submitter_email=contact_email,
        ack_subject=f"We received your claim #{claim.id}",
        ack_line=f"We've received your refund claim #{claim.id}. The team will review it and post updates "
                 "on the Refund claims page of your account. This email doesn't mean the claim has been approved.",
    )
    return _claim_out(db, claim)


@router.post("/api/user/claims/{claim_id}/", response_model=RefundClaimOut, tags=["user"],
             dependencies=[Depends(verify_csrf)])
def edit_claim(claim_id: int, payload: ClaimUserUpdateRequest, user: User = Depends(require_customer),
               db: Session = Depends(get_db)):
    claim = claim_service.update_by_user(db, claim_id=claim_id, user=user, **payload.model_dump())
    return _claim_out(db, claim)


@router.post("/api/user/claims/{claim_id}/messages/", response_model=RefundClaimOut, tags=["user"],
             dependencies=[Depends(verify_csrf)])
def message_claim(claim_id: int, payload: ClaimCustomerMessageRequest, user: User = Depends(require_customer),
                  db: Session = Depends(get_db)):
    claim = claim_service.add_customer_message(db, claim_id=claim_id, user=user, message=payload.message)
    return _claim_out(db, claim)


@router.post("/api/user/claims/{claim_id}/cancel/", response_model=RefundClaimOut, tags=["user"],
             dependencies=[Depends(verify_csrf)])
def cancel_claim(claim_id: int, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    claim = claim_service.cancel_by_user(db, claim_id=claim_id, user=user)
    return _claim_out(db, claim)


@router.post("/api/user/claims/{claim_id}/evidence/", status_code=201, tags=["user"],
             dependencies=[Depends(verify_csrf)])
async def upload_claim_evidence(
    claim_id: int,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_customer),
    db: Session = Depends(get_db),
):
    limiter = request.app.state.claim_evidence_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        from backend.errors import RateLimited

        raise RateLimited("You have uploaded several files recently. Please try again later.", limiter.retry_after(ip))
    limiter.record_failure(ip)
    data = await file.read(claim_service.MAX_EVIDENCE_BYTES + 1)
    try:
        evidence = claim_service.add_evidence(
            db,
            claim_id=claim_id,
            user=user,
            data=data,
            filename=file.filename,
            declared_type=file.content_type,
            upload_dir=request.app.state.settings.claim_upload_dir,
        )
    finally:
        await file.close()
    claim = claim_service.get_for_user(db, claim_id=claim_id, user_id=user.id)
    return _claim_out(db, claim)


@router.get("/api/user/claims/{claim_id}/evidence/{evidence_id}/", tags=["user"])
def download_claim_evidence(
    claim_id: int,
    evidence_id: int,
    request: Request,
    user: User = Depends(require_customer),
    db: Session = Depends(get_db),
):
    evidence = claim_service.get_evidence_for_user(db, claim_id=claim_id, evidence_id=evidence_id, user_id=user.id)
    path = claim_service.evidence_path(evidence, upload_dir=request.app.state.settings.claim_upload_dir)
    return FileResponse(path, filename=evidence.original_filename, media_type=evidence.content_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/api/user/messages/", status_code=201, response_model=UserMessageOut, tags=["user"],
             dependencies=[Depends(verify_csrf)])
def send_message(payload: SupportMessageRequest, request: Request, tasks: BackgroundTasks,
                 user: User = Depends(require_customer), settings: Settings = Depends(get_settings),
                 db: Session = Depends(get_db)):
    limiter = request.app.state.contact_limiter
    ip = client_ip(request)
    if limiter.is_blocked(ip):
        from backend.errors import RateLimited

        raise RateLimited("You have sent several messages recently. Please try again later.", limiter.retry_after(ip))
    message = message_service.create(db, name=user.label, email=payload.email, subject=payload.subject, body=payload.body, user=user)
    limiter.record_failure(ip)
    mail_service.queue_request_emails(
        tasks, settings,
        kind="Support message",
        subject=message.subject,
        fields=[("Message ID", f"#{message.id}"), ("Account", f"@{user.username} (#{user.id})"),
                ("Name", message.name), ("Email", message.email or "Not given"),
                ("Subject", message.subject), ("Message", message.body)],
        submitter_email=message.email,
        ack_subject="We received your support message",
        ack_line="We've received your support message. The team's reply will appear on the Support page of your account.",
    )
    return UserMessageOut.from_message(message)


@router.post("/api/user/profile/", response_model=UserSummary, tags=["user"], dependencies=[Depends(verify_csrf)])
def update_profile(payload: ProfileRequest, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return UserSummary.from_user(account_service.update_profile(db, user=user, display_name=payload.display_name))


@router.post("/api/user/password/", tags=["user"], dependencies=[Depends(verify_csrf)])
def change_password(payload: PasswordChangeRequest, request: Request, user: User = Depends(require_customer),
                    db: Session = Depends(get_db)):
    user = account_service.change_password(
        db, user=user, current=payload.current_password, new=payload.new_password, ip=client_ip(request)
    )
    # Other sessions are revoked by the version bump; keep this browser signed in.
    token = start_session(request, user)
    return {"ok": True, "csrf_token": token}


@router.get("/api/user/recovery-codes/", tags=["user"])
def recovery_codes_status(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return {"remaining": account_service.recovery_codes_remaining(db, user.id)}


@router.post("/api/user/recovery-codes/", tags=["user"], dependencies=[Depends(verify_csrf)])
def create_recovery_codes(payload: PasswordConfirmRequest, request: Request, user: User = Depends(require_customer),
                          db: Session = Depends(get_db)):
    """Replace this account's recovery codes. The codes are returned once and never shown again."""
    codes = account_service.create_recovery_codes(db, user=user, password=payload.password, ip=client_ip(request))
    return {"codes": codes, "remaining": len(codes)}


@router.get("/api/user/sign-ins/", response_model=list[SignInOut], tags=["user"])
def sign_in_history(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    return [SignInOut(created_at=e.created_at, ip=e.ip) for e in account_service.sign_in_history(db, user.id)]


@router.post("/api/user/close/", tags=["user"], dependencies=[Depends(verify_csrf)])
def close_account(payload: PasswordConfirmRequest, request: Request, user: User = Depends(require_customer),
                  db: Session = Depends(get_db)):
    account_service.close_own_account(db, user=user, password=payload.password, ip=client_ip(request))
    end_session(request)
    return {"ok": True, "redirect": "/login?closed=1"}
