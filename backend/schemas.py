"""Pydantic request and response models.

Money is exchanged as integer cents in fields whose names end in ``_cents``.
``*_display`` fields are pre-formatted USD strings for presentation only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StrictBool, StrictInt, StrictStr, field_validator

from backend.money import MAX_AMOUNT_CENTS, format_cents

REASON_MAX_LENGTH = 200


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str, when_used="json")]


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- requests ------------------------------------------------------------------


class LoginRequest(StrictModel):
    username: StrictStr = Field(max_length=64)
    password: StrictStr = Field(max_length=1024)


class LoginCodeRequest(StrictModel):
    code: StrictStr = Field(max_length=20)


class PasswordConfirmRequest(StrictModel):
    password: StrictStr = Field(max_length=1024)


class TotpEnableRequest(StrictModel):
    code: StrictStr = Field(max_length=20)


class TotpDisableRequest(StrictModel):
    password: StrictStr = Field(max_length=1024)
    code: StrictStr = Field(max_length=20)


class CreateUserRequest(StrictModel):
    username: StrictStr = Field(max_length=64)
    password: StrictStr | None = Field(default=None, max_length=1024)
    generate_password: StrictBool = False
    display_name: StrictStr | None = Field(default=None, max_length=200)


class ProfileRequest(StrictModel):
    display_name: StrictStr | None = Field(default=None, max_length=200)


class PasswordChangeRequest(StrictModel):
    current_password: StrictStr = Field(max_length=1024)
    new_password: StrictStr = Field(max_length=1024)


class SupportMessageRequest(StrictModel):
    subject: StrictStr = Field(max_length=500)
    body: StrictStr = Field(max_length=10000)
    email: StrictStr | None = Field(default=None, max_length=200)


class _ClaimFields(StrictModel):
    merchant: StrictStr = Field(max_length=120)
    order_reference: StrictStr = Field(max_length=80)
    purchase_date: date
    amount_cents: StrictInt = Field(gt=0, le=MAX_AMOUNT_CENTS)
    reason: Literal["not_received", "damaged", "incorrect_item", "duplicate_charge", "cancelled", "other"]
    description: StrictStr = Field(max_length=4000)


class ClaimCreateRequest(_ClaimFields):
    # Used once for the confirmation email and the team's notification; not stored.
    contact_email: StrictStr | None = Field(default=None, max_length=200)


class ClaimUserUpdateRequest(_ClaimFields):
    """Full replacement of the claim details while the team is waiting on the account holder."""


class ClaimCustomerMessageRequest(StrictModel):
    message: StrictStr = Field(max_length=4000)


class ClaimCreditRequest(StrictModel):
    credit_cents: StrictInt = Field(gt=0, le=MAX_AMOUNT_CENTS)
    public_reply: StrictStr | None = Field(default=None, max_length=4000)


class SavedReplyRequest(StrictModel):
    title: StrictStr = Field(max_length=200)
    body: StrictStr = Field(max_length=10000)


class ApprovalDecisionRequest(StrictModel):
    note: StrictStr | None = Field(default=None, max_length=1000)


class ClaimAdminUpdateRequest(StrictModel):
    status: Literal["under_review", "awaiting_customer", "approved", "declined", "cancelled"] | None = None
    priority: Literal["normal", "high", "urgent"] | None = None
    assign_to_user_id: StrictInt | None = None
    assign_to_me: StrictBool = False
    public_reply: StrictStr | None = Field(default=None, max_length=4000)
    internal_note: StrictStr | None = Field(default=None, max_length=4000)


class MessageUpdateRequest(StrictModel):
    status: Literal["new", "read", "archived"] | None = None
    reply: StrictStr | None = Field(default=None, max_length=10000)


class AnnouncementRequest(StrictModel):
    title: StrictStr = Field(max_length=500)
    body: StrictStr = Field(max_length=10000)
    level: Literal["info", "success", "warning"] = "info"
    published: StrictBool = True
    pinned: StrictBool = False


class NoteRequest(StrictModel):
    body: StrictStr = Field(max_length=5000)


class SettingsRequest(StrictModel):
    registration_open: StrictBool | None = None
    public_notice: StrictStr | None = Field(default=None, max_length=1000)
    approval_threshold_cents: StrictInt | None = Field(default=None, ge=0, le=MAX_AMOUNT_CENTS)
    stale_claim_days: StrictInt | None = Field(default=None, ge=1, le=365)


class BulkRequest(StrictModel):
    user_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    action: Literal["adjust", "activate", "deactivate"]
    amount_cents: StrictInt | None = Field(default=None, ge=-MAX_AMOUNT_CENTS, le=MAX_AMOUNT_CENTS)
    reason: StrictStr | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return _clean_reason(value)


class StatusRequest(StrictModel):
    status: Literal["active", "inactive"]


def _clean_reason(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    if not value:
        return None
    if len(value) > REASON_MAX_LENGTH:
        raise ValueError(f"Reason must be at most {REASON_MAX_LENGTH} characters.")
    return value


class _BalanceRequestBase(StrictModel):
    reason: StrictStr | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return _clean_reason(value)


class SetBalanceRequest(_BalanceRequestBase):
    mode: Literal["set"]
    target_cents: StrictInt = Field(ge=-MAX_AMOUNT_CENTS, le=MAX_AMOUNT_CENTS)


class AdjustBalanceRequest(_BalanceRequestBase):
    mode: Literal["adjust"]
    amount_cents: StrictInt = Field(ge=-MAX_AMOUNT_CENTS, le=MAX_AMOUNT_CENTS)

    @field_validator("amount_cents")
    @classmethod
    def _non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Adjustment amount must not be zero.")
        return value


BalanceRequest = Annotated[Union[SetBalanceRequest, AdjustBalanceRequest], Field(discriminator="mode")]


# --- responses -----------------------------------------------------------------


class UserSummary(OutModel):
    id: int
    username: str
    display_name: str | None = None
    role: str
    status: str
    balance_cents: int
    balance_display: str = ""
    signup_source: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    last_login_at: UtcDatetime | None = None

    @classmethod
    def from_user(cls, user) -> "UserSummary":
        out = cls.model_validate(user)
        out.balance_display = format_cents(user.balance_cents)
        return out


class ActivityOut(OutModel):
    id: int
    created_at: UtcDatetime
    change_type: str
    previous_cents: int
    amount_cents: int
    resulting_cents: int
    reason: str | None
    claim_id: int | None = None
    claim_merchant: str | None = None

    @classmethod
    def from_activity(cls, activity):
        out = cls.model_validate(activity)
        if activity.claim_id is not None and "claim" in activity.__dict__ and activity.claim is not None:
            out.claim_merchant = activity.claim.merchant
        return out


class AdminActivityOut(ActivityOut):
    user_id: int
    admin_id: int
    admin_username: str | None = None
    username: str | None = None
    approved_by_id: int | None = None
    approved_by_username: str | None = None

    @classmethod
    def from_activity(cls, activity) -> "AdminActivityOut":
        out = super().from_activity(activity)
        out.admin_username = activity.admin.username if activity.admin else None
        if "user" in activity.__dict__ and activity.user is not None:
            out.username = activity.user.username
        if activity.approved_by_id is not None and activity.approved_by is not None:
            out.approved_by_username = activity.approved_by.username
        return out


class MessageOut(OutModel):
    id: int
    user_id: int | None
    username: str | None = None
    name: str
    email: str | None
    subject: str
    body: str
    status: str
    reply: str | None
    replied_at: UtcDatetime | None
    created_at: UtcDatetime

    @classmethod
    def from_message(cls, message) -> "MessageOut":
        out = cls.model_validate(message)
        if "user" in message.__dict__ and message.user is not None:
            out.username = message.user.username
        return out


class UserMessageOut(OutModel):
    id: int
    subject: str
    body: str
    status: str
    reply: str | None
    replied_at: UtcDatetime | None
    created_at: UtcDatetime
    reply_unread: bool = False

    @classmethod
    def from_message(cls, message) -> "UserMessageOut":
        out = cls.model_validate(message)
        out.reply_unread = bool(message.reply) and message.reply_seen_at is None
        return out


class ClaimEventOut(OutModel):
    id: int
    event_type: str
    from_status: str | None
    to_status: str | None
    message: str | None
    is_internal: bool = False
    created_at: UtcDatetime
    actor_label: str | None = None

    @classmethod
    def from_event(cls, event) -> "ClaimEventOut":
        out = cls.model_validate(event)
        out.actor_label = event.actor.display_name or event.actor.username if event.actor else None
        return out


class ClaimEvidenceOut(OutModel):
    id: int
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: UtcDatetime
    download_path: str = ""

    @classmethod
    def from_evidence(cls, evidence, *, claim_id: int, is_admin: bool = False) -> "ClaimEvidenceOut":
        out = cls.model_validate(evidence)
        prefix = "/api/admin" if is_admin else "/api/user"
        out.download_path = f"{prefix}/claims/{claim_id}/evidence/{evidence.id}/"
        return out


class ClaimDuplicateOut(BaseModel):
    id: int
    user_id: int
    username: str | None = None
    status: str
    amount_cents: int
    created_at: UtcDatetime


class RefundClaimOut(OutModel):
    id: int
    user_id: int
    username: str | None = None
    display_name: str | None = None
    merchant: str
    order_reference: str
    purchase_date: date
    amount_cents: int
    reason: str
    description: str
    status: str
    priority: str
    assigned_admin_id: int | None
    assigned_admin_name: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    events: list[ClaimEventOut] = Field(default_factory=list)
    evidence: list[ClaimEvidenceOut] = Field(default_factory=list)
    # Derived values (claim_service.annotate).
    status_since: UtcDatetime | None = None
    credit_cents: int | None = None
    credit_activity_id: int | None = None
    pending_credit_cents: int | None = None
    pending_request_id: int | None = None
    overdue: bool = False
    stale: bool = False
    possible_duplicate: bool = False
    duplicates: list[ClaimDuplicateOut] = Field(default_factory=list)
    has_update: bool = False

    @classmethod
    def from_claim(cls, claim, *, include_internal: bool = False, extra: dict | None = None) -> "RefundClaimOut":
        out = cls.model_validate(claim)
        for key, value in (extra or {}).items():
            setattr(out, key, [ClaimDuplicateOut(**d) for d in value] if key == "duplicates" else value)
        out.username = claim.user.username if claim.user else None
        out.display_name = claim.user.display_name if claim.user else None
        out.assigned_admin_name = (
            claim.assigned_admin.display_name or claim.assigned_admin.username if claim.assigned_admin else None
        )
        events = [event for event in claim.events if include_internal or not event.is_internal]
        out.events = [ClaimEventOut.from_event(event) for event in events]
        out.evidence = [
            ClaimEvidenceOut.from_evidence(item, claim_id=claim.id, is_admin=include_internal)
            for item in claim.evidence
        ]
        return out


class AnnouncementOut(OutModel):
    id: int
    title: str
    body: str
    level: str
    published: bool
    pinned: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class NoteOut(OutModel):
    id: int
    body: str
    author_id: int | None
    author_username: str | None = None
    created_at: UtcDatetime

    @classmethod
    def from_note(cls, note) -> "NoteOut":
        out = cls.model_validate(note)
        out.author_username = note.author.username if note.author else None
        return out


class AuditOut(OutModel):
    id: int
    action: str
    action_label: str = ""
    actor_id: int | None
    actor_username: str | None = None
    target_user_id: int | None
    target_username: str | None = None
    details: str | None
    ip: str | None
    created_at: UtcDatetime

    @classmethod
    def from_entry(cls, entry, labels: dict) -> "AuditOut":
        out = cls.model_validate(entry)
        out.action_label = labels.get(entry.action, entry.action)
        out.actor_username = entry.actor.username if entry.actor else None
        out.target_username = entry.target.username if entry.target else None
        return out


class Page(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int


class ActivityPage(Page):
    items: list[ActivityOut]


class AdminActivityPage(Page):
    items: list[AdminActivityOut]


class UserPage(Page):
    items: list[UserSummary]


class MessagePage(Page):
    items: list[MessageOut]


class RefundClaimPage(Page):
    items: list[RefundClaimOut]


class ClaimReasonStat(BaseModel):
    reason: str
    label: str
    total: int
    approved: int
    declined: int


class ClaimMerchantStat(BaseModel):
    merchant: str
    total: int
    amount_cents: int


class ClaimMetricsOut(BaseModel):
    open: int
    submitted: int
    awaiting_customer: int
    urgent: int
    overdue_48h: int
    average_first_reply_minutes: int | None
    average_resolution_minutes: int | None
    approval_rate_percent: int | None = None
    credited_count: int = 0
    credited_cents: int = 0
    by_reason: list[ClaimReasonStat] = Field(default_factory=list)
    top_merchants: list[ClaimMerchantStat] = Field(default_factory=list)


class ClaimAssigneeOut(BaseModel):
    id: int
    username: str
    display_name: str | None = None


class AuditPage(Page):
    items: list[AuditOut]


class LoginResponse(BaseModel):
    user: UserSummary
    redirect: str
    csrf_token: str


class CreateUserResponse(BaseModel):
    user: UserSummary
    temporary_password: str | None = None


class UserDetailResponse(BaseModel):
    user: UserSummary
    activity: AdminActivityPage


class BalanceResponse(BaseModel):
    user: UserSummary
    activity: AdminActivityOut
    previous_cents: int
    amount_cents: int
    resulting_cents: int
    replayed: bool = False


class StatsResponse(BaseModel):
    total_users: int
    active_users: int
    inactive_users: int
    total_balance_cents: int
    total_balance_display: str
    recent_activity_count: int
    recent_activity_window_days: int
    total_activity_count: int
    new_users_7d: int
    average_balance_cents: int
    unread_messages: int


class SettingsOut(BaseModel):
    registration_open: bool
    public_notice: str
    approval_threshold_cents: int = 0
    stale_claim_days: int = 14


class SavedReplyOut(OutModel):
    id: int
    title: str
    body: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class PendingChangeOut(OutModel):
    id: int
    user_id: int
    username: str | None = None
    requested_by_id: int
    requested_by_username: str | None = None
    mode: str
    value_cents: int
    balance_at_request_cents: int
    reason: str | None
    claim_id: int | None
    status: str
    decided_by_username: str | None = None
    decided_at: UtcDatetime | None
    decision_note: str | None
    activity_id: int | None
    created_at: UtcDatetime
    current_balance_cents: int | None = None

    @classmethod
    def from_pending(cls, pending) -> "PendingChangeOut":
        out = cls.model_validate(pending)
        out.username = pending.user.username if pending.user else None
        out.requested_by_username = pending.requested_by.username if pending.requested_by else None
        out.decided_by_username = pending.decided_by.username if pending.decided_by else None
        out.current_balance_cents = pending.user.balance_cents if pending.user else None
        return out


class ApprovalListOut(BaseModel):
    pending: list[PendingChangeOut]
    recent: list[PendingChangeOut]


class SignInOut(BaseModel):
    created_at: UtcDatetime
    ip: str | None


class HistoryPoint(BaseModel):
    at: UtcDatetime
    balance_cents: int


class UserSummaryStats(BaseModel):
    balance_cents: int
    total_credits_cents: int
    total_debits_cents: int
    change_count: int
    first_activity_at: UtcDatetime | None
    history: list[HistoryPoint]


class OkResponse(BaseModel):
    ok: bool = True
