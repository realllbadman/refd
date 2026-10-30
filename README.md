# RefundClaimers

RefundClaimers is a private classroom prototype: a full-stack account platform that runs locally. Visitors land on a public website (Home, About us, FAQ, Contact, Terms), create an account, and get a personal dashboard with their balance, history, charts, announcements and support messages. Administrators manage everything from a separate control panel: accounts, balances, bulk actions, a ledger of all changes, a support inbox, announcements, an audit log and site settings.

It is a working application: a FastAPI backend, persistent SQLite storage, signed-cookie sessions, server-side authorisation, an atomic balance engine and server-rendered pages enhanced with vanilla JavaScript.

> Balances here are **application-recorded ledger values**. RefundClaimers has no banking, card or payment integration, so a balance change is an internal ledger adjustment and never an external transfer.

---

## Features

### Public website
- **Pages:** Home, About us, FAQ, Contact, Terms & privacy, Sign in and Create account. Every page has its own URL to share, and the footer has a **Copy link to this page** button.
- **Self-registration** at `/register`: username, optional display name and password. No email, ID or bank details. New accounts are always active account holders with a $0.00 balance, and are signed in straight away.
- **Contact form** stores messages for the administrators. It has CSRF protection, a spam honeypot and a rate limit.
- Administrators can **close sign-ups** and show a **site-wide notice** banner.

### Account-holder area
- **Overview:** balance card with totals, balance-history chart, pinned announcements, account details and recent activity.
- **Activity:** full history with type and increase/decrease filters.
- **Refund claims:** submit a request with merchant, order reference, purchase date, amount, reason and up to five supporting documents; follow a step-by-step progress tracker with "what happens next" guidance; message the review team, add documents, edit the details when the team asks for more information, and cancel an open request. Claims with a new team update are highlighted, and an unread badge appears in the sidebar.
- **Activity:** rows credited from an approved claim link back to that claim. **Download CSV** exports the full history.
- **Support:** send messages and read the team's replies (new replies are badged until seen).
- **Profile & security:** display name; password change (signs out every other device); **recovery codes** (eight one-time codes for resetting a forgotten password at `/forgot-password`); recent sign-ins with IP; and **close my account** (password required; history is kept and an administrator can reopen it).

### Administrator panel
- **Overview:** 8 live stat tiles, sign-ups per day and net balance change per day charts (7/30/90 days), latest changes, newest accounts, highest balances and quick actions.
- **Accounts:** search, filter by status and role, six sort orders, multi-select **bulk adjust / activate / deactivate**, CSV export and account creation.
- **Account detail:** set/adjust balance, activate/deactivate, **reset password** (one-time temporary password), **internal notes**, history CSV export, last sign-in and sign-up source.
- **Refund claims:** searchable review queue with status and priority filters, decision updates, public replies and private review notes. Also:
  - **Approve and credit:** approves the claim and credits the account (up to the claimed amount) in one transaction. The ledger entry stores the claim ID, and a claim can only be credited once (enforced by a unique index).
  - **Queues:** All, Assigned to me, Unassigned, No update in 48h, and Stale, with counts. Assign a claim to yourself or a colleague from the detail view.
  - **Aging:** time in current status on every row, plus "No update 48h+" and "Stale" flags. **Close stale claims** cancels claims that have been awaiting the customer longer than the configured number of days, with a note to the account holder.
  - **Possible duplicates:** claims with the same merchant and order reference (case-insensitive) are flagged and linked to each other.
  - **Metrics and insights:** open, overdue and awaiting counts, average first reply and decision time, approval rate, total credited, a breakdown by reason, and the most-claimed merchants.
- **Saved replies:** reusable text, managed under Settings, inserted from a picker when replying to claims or messages.
- **Second-admin approval:** with a threshold set under Settings, any balance change, bulk adjustment or claim credit at or above it waits on the **Approvals** page for a *different* administrator. The ledger records both the requester and the approver. Closing a claim withdraws its pending credit.
- **My security:** optional two-step sign-in (TOTP authenticator app) for administrators. Another administrator can reset it from the account page.
- **Activity ledger:** every change across all accounts, filtered by account, type, direction and date range; CSV export.
- **Messages:** inbox with Open, Unread, Archived and All tabs, reply (account holders see replies), archive and mark unread. An unread badge appears in the sidebar.
- **Announcements:** create, edit, pin, publish/unpublish and delete.
- **Audit log:** append-only record of sign-ins, registrations, account changes, balance changes, messages, announcements and settings changes.
- **Settings:** registration on/off, public notice, approval threshold, stale-claim days, saved replies, and shareable links to every public page.

### Core

- **One sign-in page for both roles.** The role comes from the database, never from the request.
- **Account creation** with a typed or securely generated temporary password. A generated password is shown once and never stored in readable form. New accounts always start **active**, with the **user** role and a **$0.00** balance.
- **Balance engine**
  - **Set** mode moves the balance to a target value; **adjust** mode applies a signed change.
  - All arithmetic uses integer cents. Floats are never used.
  - Each change updates the balance and writes its activity record **in one transaction**.
  - Concurrent changes are serialised by a row write lock plus a compare-and-swap guard, so updates cannot be lost.
  - An optional `Idempotency-Key` header makes retries safe.
  - Negative resulting balances are rejected unless `ALLOW_NEGATIVE=1`.
- **Append-only activity log**
  - Each change records the account, the acting administrator, the operation type, the previous balance, the signed change, the resulting balance, an optional reason and a UTC timestamp.
  - No endpoint edits or deletes history, and SQLite triggers reject `UPDATE`/`DELETE` on the table.
  - A database `CHECK` constraint enforces `resulting = previous + change`.
- **Account status:** an administrator can deactivate or reactivate an account. Balance and history are always kept, and deactivating an account ends its existing sessions immediately. You cannot deactivate yourself or the last active administrator.
- **Refund claim history:** customer and administrator updates are stored in a claim timeline. Internal review notes are omitted from customer responses, and claim changes are included in the audit log.
- **Admin dashboard**
  - Live statistics from the database.
  - Search by username or ID, with pagination.
  - Per-row actions and a detail page with full history.
  - A confirmation dialog that previews every balance change.
- **Account-holder dashboard:** the user's balance, account details and paginated activity, with loading, empty and error states.
- **Security**
  - bcrypt password hashing that rejects passwords over 72 bytes instead of silently truncating them.
  - HttpOnly, SameSite=Lax signed session cookies.
  - CSRF tokens on every state-changing request.
  - Per-IP login rate limiting.
  - A strict Content-Security-Policy (no inline scripts or styles) and `Cache-Control: no-store` on pages and APIs. Visitors and signed-in account holders also get the Smartsupp live chat, except on pages with password fields (Sign in, Create account, password reset, Profile & security). Administrators never load it. Only responses that load the chat get a policy that allows Smartsupp's origins and inline styles.
  - Error responses never include stack traces, SQL or submitted passwords.
- **Responsive and accessible**
  - Semantic HTML and labelled fields.
  - Native `<dialog>` modals with focus handling.
  - Visible focus rings.
  - Positive and negative amounts carry a sign and an icon as well as colour.
  - Tables become stacked cards on phones.
  - Reduced-motion preferences are respected.

## Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+ (tested on 3.10.12; written for 3.12 compatibility), FastAPI, Uvicorn |
| Data | SQLAlchemy 2 ORM, SQLite (WAL mode, foreign keys on) |
| Validation | Pydantic v2 |
| Auth | passlib[bcrypt] (bcrypt 4.0.1), Starlette `SessionMiddleware` (itsdangerous-signed cookies) |
| Frontend | Jinja2 templates, HTML5, CSS3 (custom design tokens), vanilla JavaScript + Fetch API |
| Tests | pytest, FastAPI `TestClient` |

## Project structure

```
.
├── backend/
│   ├── main.py               # app factory, middleware, startup (entry point: backend.main:app)
│   ├── config.py             # loads .env first, validates settings
│   ├── database.py           # engine, session factory, get_db dependency
│   ├── models.py             # User, BalanceActivity, ContactMessage, Announcement, AccountNote, AuditEntry, AppSetting
│   ├── schemas.py            # Pydantic request/response models
│   ├── security.py           # hashing, sessions, CSRF, login rate limiter
│   ├── dependencies.py       # get_current_user, require_user/customer/admin, CSRF checks
│   ├── errors.py             # exception types + JSON/HTML error handlers
│   ├── money.py              # exact cents parsing/formatting
│   ├── bootstrap.py          # initial admin + optional demo seed
│   ├── templating.py         # Jinja2 environment and render helper
│   ├── services/
│   │   ├── account_service.py       # create/register, list/search/sort, status, passwords, notes, stats, charts
│   │   ├── balance_service.py       # atomic set/adjust engine + bulk adjust
│   │   ├── activity_service.py      # per-account and global ledger queries
│   │   ├── message_service.py       # contact/support messages and replies
│   │   ├── claim_service.py         # claim intake, status transitions and review timeline
│   │   ├── announcement_service.py  # dashboard announcements
│   │   ├── audit_service.py         # append-only audit log
│   │   └── settings_service.py      # registration switch, public notice
│   ├── routers/
│   │   ├── public.py         # /, /about, /faq, /terms, /contact, /register
│   │   ├── auth.py           # /login, /logout, /api/login/, /api/logout/, /api/me/, /api/csrf/
│   │   ├── user.py           # /dashboard/*, /api/user/*
│   │   └── admin.py          # /admin/*, /api/admin/*
│   ├── templates/
│   │   ├── base.html         # app shell (sidebar) for signed-in pages, website layout for visitors
│   │   ├── public/           # home, about, faq, contact, terms, register
│   │   ├── user/             # activity, claims, support, profile
│   │   ├── admin/            # overview, claims, activity, messages, announcements, audit, settings
│   │   └── …                 # login, dashboard, admin (accounts), admin_user_detail, dialogs, icons, 403/404/500
│   └── static/
│       ├── css/styles.css
│       ├── js/app.js         # shared API client, formatting, dialogs, activity table, copy-link
│       ├── js/charts.js      # dependency-free SVG column/line charts with tooltips + table view
│       ├── js/public.js      # sign-up and contact forms
│       ├── js/login.js
│       ├── js/dashboard.js   # account-holder pages
│       ├── js/claims.js      # account-holder claim submission and tracking
│       ├── js/admin.js       # accounts, account detail, balance dialog, bulk actions
│       ├── js/admin-pages.js # admin overview, claims queue, ledger, messages, announcements, audit, settings
│       └── images/logo.svg
├── tests/                    # pytest suite (isolated temp databases)
├── .env.example
├── requirements.txt
└── README.md
```

## Prerequisites

- Python **3.10 or newer** (3.12 recommended).
- `pip`, plus either the `venv` module or `virtualenv`.

## Setup

```bash
cd /path/to/project

# 1. Create and activate a virtual environment
python3 -m venv .venv            # or: python3 -m virtualenv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure the environment
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as SECRET_KEY
# edit .env: set SECRET_KEY and a strong ADMIN_PASSWORD (12+ characters)
```

On Debian/Ubuntu, if `python3 -m venv` fails with *ensurepip is not available*, either install `python3-venv` (`sudo apt install python3.10-venv`) or use `python3 -m pip install --user virtualenv && python3 -m virtualenv .venv`.

### `.env` reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | yes | — | Signs session cookies. At least 32 characters; the placeholder value is rejected. |
| `ADMIN_USERNAME` | first run | — | Username of the initial administrator. |
| `ADMIN_PASSWORD` | first run | — | Password of the initial administrator. At least 12 characters and at most 72 bytes; the placeholder value is rejected. |
| `DATABASE_URL` | no | `sqlite:///./ledger.db` | Database location. A relative SQLite path is resolved from the project root, not the current directory. |
| `ALLOW_NEGATIVE` | no | `0` | `1` allows balances to go below $0.00. |
| `COOKIE_SECURE` | no | `0` | `1` adds the `Secure` flag to the session cookie. Use it when serving over HTTPS. |
| `SESSION_MAX_AGE` | no | `1209600` | Session lifetime in seconds (14 days). |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW` | no | `10` / `300` | Number of failed sign-ins allowed per IP within the window (seconds). |
| `SEED_DEMO` | no | `0` | `1` creates fictional demo accounts at startup (see below). |
| `SITE_NAME` | no | `RefundClaimers` | Name shown in the header, footer and page titles. |
| `SUPPORT_EMAIL` | no | `support@refundclaimers.com` | Address shown on the site and sent form notifications. |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | to send email | — | Mail server login. If any is missing, sending is off: forms still work and each skipped email is logged. |
| `SMTP_PORT` / `SMTP_SECURE` | no | `465` / `true` | `true` uses implicit TLS; `false` uses STARTTLS. Plain, unencrypted SMTP is not supported. |
| `MAIL_FROM` | no | the support address | From header, e.g. `"RefundClaimers <support@refundclaimers.com>"`. |

`.env`, `*.db` and the SQLite WAL/SHM files are listed in `.gitignore`. Never commit them.

### Initial administrator provisioning

At startup the app checks whether any administrator exists:

- **None exists:** it creates one from `ADMIN_USERNAME` / `ADMIN_PASSWORD`. If those are missing, still the placeholder, or too weak, startup stops with a message explaining what to fix.
- **One exists:** nothing happens. Changing `ADMIN_PASSWORD` later does **not** reset the existing administrator's password, and bootstrap never overwrites account data.

### Database initialisation

Tables are created on first start with `create_all()`, which only creates missing tables and never drops or alters existing ones. Users, balances and history persist across restarts in `ledger.db`.

**Automatic additive upgrade:** columns added in later versions (`users.display_name`, `users.last_login_at`, `users.signup_source`) are added to an older database at startup by `database.ensure_columns()`. This step only adds nullable columns and never rewrites data. New tables (messages, announcements, notes, audit log, settings) are created automatically.

**Schema changes:** `create_all()` will not alter existing tables. If you change the models, add a migration tool such as Alembic (`alembic init`, autogenerate a revision, review it, then `alembic upgrade head`), or back up `ledger.db` and apply `ALTER TABLE` statements manually. Do not delete the database to "migrate" it.

## Running the application

From the project root:

```bash
./run.sh
```

`run.sh` starts `uvicorn backend.main:app --reload --port 8015` from the virtual environment. If an earlier RefundClaimers server is still running on port 8015, the script stops it first, so you never get *Address already in use*. If a different program is using the port, it shows which one and exits without touching it. To use another port: `PORT=8020 ./run.sh`.

The equivalent manual command, with the virtual environment active:

```bash
uvicorn backend.main:app --reload --port 8015
```

Open <http://127.0.0.1:8015>. The public home page loads first.

- **Visitors** click **Create account**, or open the shared link `http://<host>:8015/register`. After signing up they land on **/dashboard**.
- **The administrator** clicks **Sign in** and uses `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`. Administrators land on **/admin**.

A typical demo:

1. Open the home page in one browser and create an account.
2. In a second browser (or a private window), sign in as the administrator, open **Accounts**, and use **Set** or **Adjust** on the new account. Or select several accounts and use **Adjust balances**.
3. Post an **announcement**, then refresh the user's dashboard: the balance, chart, history and announcement all update.
4. Send a message from the user's **Support** page and reply to it from the admin **Messages** inbox.

To let other devices on your network open the site, start it with `--host 0.0.0.0`, for example `uvicorn backend.main:app --host 0.0.0.0 --port 8015`, and share `http://<your-computer's-IP>:8015`.

## Email

The Contact, Support and Refund claim forms each send the team a notification with every field, with `Reply-To` set to the submitter's address, and send the submitter a confirmation. The email field is optional, and no confirmation is sent without one. Confirmations repeat nothing the submitter typed, because anyone can enter someone else's address.

Sending happens after the response, so a slow or failing mail server never affects the form. Failures are logged on the server and never shown to the visitor. The admin **Settings** page shows whether outgoing email is on.

To test delivery, run this from the project root. It sends one message straight away and prints the real result or SMTP error:

```bash
.venv/bin/python -m backend.send_test_email you@example.com
```

## Running the tests

```bash
pytest -q
```

The suite sets `LEDGER_SKIP_DOTENV=1` and gives every test its own temporary SQLite file, so it never reads your `.env` or touches `ledger.db`. It covers:

- **Authentication:** sign-in, invalid credentials, inactive accounts, logout, CSRF, rate limiting and redirects.
- **Authorisation:** user/admin separation, cross-account access, tampered cookies, payload-based privilege escalation and revoked sessions.
- **Account management:** account creation and validation, search and pagination, statistics, status changes, the last-administrator guard and append-only triggers.
- **Balance engine:** set/adjust, the negative-balance policy, invalid amounts, rollback when the activity write fails, idempotent replays and concurrent updates from multiple threads.
- **Refund claims:** submission validation, customer ownership, role boundaries, status changes, customer cancellation, admin assignment and private-note visibility.
- **Persistence and bootstrap:** data survives a restart, and the bootstrap rules behave as described above.

## API reference

All JSON endpoints live under `/api/` and **use a trailing slash**. The app disables automatic slash redirects, so `/api/me` returns 404 rather than redirecting. This avoids silent POST→GET redirect problems.

**Money:** every amount is an integer number of cents in a field ending in `_cents` (`"balance_cents": 125050` means $1,250.50). Some responses also include a `*_display` string such as `"$1,250.50"` for presentation only.

**Timestamps:** ISO-8601 UTC strings, for example `"2026-10-26T19:58:43.075000Z"`.

**Authentication:** a signed `ledger_session` cookie. Every `POST` must send the session's CSRF token in the `X-CSRF-Token` header. Pages embed the token in `<meta name="csrf-token">`, and API clients can fetch it from `GET /api/csrf/`.

**Errors** always have this shape:

```json
{"error": {"code": "negative_balance", "message": "This change would make the balance -$10.00. Negative balances are not permitted.",
           "details": [{"field": "amount_cents", "message": "..."}]}}
```

| Status | Meaning |
|---|---|
| 400 | Bad request (form sign-in with an expired form). |
| 401 | `not_authenticated` / `invalid_credentials`: no valid session, or wrong username or password. |
| 403 | `forbidden` / `csrf_failed` / `account_unavailable`: wrong role, bad CSRF token, or inactive account. |
| 404 | `not_found` |
| 409 | `username_taken`, `negative_balance`, `self_deactivation`, `last_admin`, `not_customer_account`, `idempotency_mismatch`, `balance_busy` |
| 422 | `validation_error`, `no_change`, `out_of_range` |
| 429 | `rate_limited`, with a `Retry-After` header. |
| 500 | `server_error` (details are logged on the server only). |

### Auth

| Method & path | Who | Description |
|---|---|---|
| `GET /api/csrf/` | anyone | `{"csrf_token": "..."}` |
| `POST /api/login/` | anyone | Body `{"username", "password"}`. Returns `{"user": UserSummary, "redirect": "/admin" or "/dashboard", "csrf_token"}`. The session is rotated on login. For an account with two-step sign-in it returns **202** `{"two_factor_required": true, "csrf_token"}` instead, and nothing is signed in yet. |
| `POST /api/login/code/` | password step done | Body `{"code"}` (6 digits, within 5 minutes). Returns the same body as `/api/login/`. A code can't be reused. |
| `POST /api/logout/` | signed in | Returns `{"ok": true}` and clears the session. |
| `GET /api/me/` | signed in | Returns `UserSummary`. |

`UserSummary` = `{id, username, role, status, balance_cents, balance_display, created_at, updated_at}`

### Pages

Public: `/`, `/about`, `/faq`, `/contact` (GET/POST form), `/terms`, `/register` (GET/POST form), `/login`, `/login/code` (two-step sign-in), `/forgot-password` (GET/POST form), `/logout`.
Account holder: `/dashboard`, `/dashboard/activity`, `/dashboard/claims`, `/dashboard/support`, `/dashboard/profile`.
Administrator: `/admin`, `/admin/accounts`, `/admin/users/{id}`, `/admin/claims` (`?claim={id}` opens one), `/admin/approvals`, `/admin/activity`, `/admin/messages`, `/admin/announcements`, `/admin/audit`, `/admin/settings`, `/admin/security`.

### Account holder

| Method & path | Description |
|---|---|
| `GET /api/user/activity/?page=1&page_size=20&type=&direction=` | The caller's own activity, newest first. `type` is `set` or `adjust`; `direction` is `credit` or `debit`. The account is always taken from the session. Returns `{items: [Activity], page, page_size, total, pages}`. |
| `GET /api/user/summary/` | `{balance_cents, total_credits_cents, total_debits_cents, change_count, first_activity_at, history: [{at, balance_cents}]}` (last 60 changes). |
| `GET /api/user/announcements/` | Published announcements, pinned first (maximum 5). |
| `GET /api/user/messages/` · `POST /api/user/messages/` | List your own support messages with replies, or send one: `{"subject", "body"}`. |
| `POST /api/user/profile/` | `{"display_name"}` |
| `POST /api/user/password/` | `{"current_password", "new_password"}`. Revokes every other session and returns a new `csrf_token` for this one. |
| `GET /api/user/activity/export.csv` | The caller's full history as CSV. |
| `GET /api/user/claims/` · `POST /api/user/claims/` | Your claims (with `has_update`, `credit_cents`, `status_since`), or submit one. |
| `POST /api/user/claims/{id}/` | Replace the claim details (same body as creating). Only while the status is `awaiting_customer`; the change is posted to the claim timeline. |
| `POST /api/user/claims/{id}/messages/` · `…/cancel/` · `…/evidence/` | Message the team, cancel, or upload a document (multipart `file`). |
| `POST /api/user/claims/seen/` · `POST /api/user/messages/seen/` | Mark claim updates or support replies as seen (clears the badges). |
| `GET /api/user/recovery-codes/` · `POST /api/user/recovery-codes/` | `{"remaining"}`, or `{"password"}` → `{"codes": [8 codes]}`. Creating codes replaces the old ones; the codes are shown once. |
| `GET /api/user/sign-ins/` | The last 10 sign-ins: `[{created_at, ip}]`. |
| `POST /api/user/close/` | `{"password"}`. Deactivates the account, signs out everywhere and returns `{"redirect": "/login?closed=1"}`. |

`Activity` = `{id, created_at, change_type: "set"|"adjust", previous_cents, amount_cents, resulting_cents, reason, claim_id, claim_merchant}`

### Administrator

| Method & path | Description |
|---|---|
| `GET /api/admin/stats/` | `{total_users, active_users, inactive_users, total_balance_cents, total_balance_display, recent_activity_count, recent_activity_window_days, total_activity_count}`. User counts include account holders only. The total is the **sum of balances recorded in this ledger**, not a statement of funds held anywhere. "Recent" means the last 7 days. |
| `GET /api/admin/users/?q=&page=1&page_size=20` | Lists all accounts (including administrators), newest first. `q` matches part of a username or an exact numeric ID (`42` or `#42`). Returns `{items: [UserSummary], page, page_size, total, pages}`. |
| `POST /api/admin/users/` | Body `{"username", "password"}` **or** `{"username", "generate_password": true}`. Returns **201** `{"user": UserSummary, "temporary_password": string|null}`. Any other field, such as `role`, `status` or `balance_cents`, is rejected with 422. |
| `GET /api/admin/users/{id}/?page=&page_size=` | `{"user": UserSummary, "activity": {items: [AdminActivity], ...}}` |
| `GET /api/admin/users/{id}/activity/?page=&page_size=` | A paginated list of `AdminActivity`. |
| `POST /api/admin/users/{id}/status/` | Body `{"status": "active"|"inactive"}`. Returns `UserSummary`. |
| `POST /api/admin/users/{id}/balance/` | See below. |
| `GET /api/admin/users/?status=&role=&sort=` | Extra filters. `sort` is one of `newest`, `oldest`, `balance_desc`, `balance_asc`, `username`, `last_login`. |
| `GET /api/admin/users/export.csv` | Accounts as CSV (same filters). Free-text cells are prefixed with `'` so spreadsheet formulas can't run. |
| `POST /api/admin/users/bulk/` | `{"user_ids": [..], "action": "adjust"|"activate"|"deactivate", "amount_cents"?, "reason"?}`. Each account is processed in its own transaction; returns `{succeeded, failed, results: [...]}`. |
| `POST /api/admin/users/{id}/password-reset/` | Returns a one-time `temporary_password` and signs the user out everywhere. |
| `GET` / `POST /api/admin/users/{id}/notes/` · `DELETE …/notes/{note_id}/` | Internal notes (administrators can delete only their own). |
| `GET /api/admin/overview/` | Recent activity, newest accounts and top balances. |
| `GET /api/admin/charts/?days=30` | Zero-filled per-day series (UTC): `signups`, `credits_cents`, `debits_cents`, `net_cents`, `changes`. |
| `GET /api/admin/activity/?q=&type=&direction=&from=&to=` · `GET /api/admin/activity/export.csv` | Global ledger; `export.csv` also accepts `user_id`. |
| `GET /api/admin/messages/?status=open|new|read|archived` · `POST /api/admin/messages/{id}/` | Inbox; update with `{"status"?, "reply"?}`. |
| `GET` / `POST /api/admin/announcements/` · `POST` / `DELETE /api/admin/announcements/{id}/` | `{"title", "body", "level": "info"|"success"|"warning", "published", "pinned"}` |
| `GET /api/admin/audit/?action=&q=` | Audit events. `action` is a category (`auth`, `account`, `balance`, `message`, `announcement`, `settings`) or an exact action. |
| `GET` / `POST /api/admin/settings/` | `{"registration_open", "public_notice", "approval_threshold_cents", "stale_claim_days"}`. A non-zero threshold needs at least two active administrators (**409** `needs_second_admin`). |
| `GET /api/admin/claims/?queue=&status=&priority=&q=` · `GET /api/admin/claims/export.csv` | Review queue. `queue` is `mine`, `unassigned`, `overdue` or `stale`. Each claim includes `status_since`, `overdue`, `stale`, `possible_duplicate`, `credit_cents` and `pending_credit_cents`. |
| `GET /api/admin/claims/{id}/` · `POST /api/admin/claims/{id}/` | Detail (adds a `duplicates` list); update with `{"status"?, "priority"?, "assign_to_user_id"?, "assign_to_me"?, "public_reply"?, "internal_note"?}`. |
| `POST /api/admin/claims/{id}/credit/` | `{"credit_cents", "public_reply"?}`. Approves and credits in one transaction: **201** `{"claim", "pending": null}`. At or above the approval threshold: **202** `{"claim", "pending": PendingChange}`. |
| `GET /api/admin/claims/metrics/` · `GET /api/admin/claims/queues/` · `POST /api/admin/claims/close-stale/` | Metrics and insights; queue counts; close stale claims (returns `{"closed": n}`). |
| `GET` / `POST /api/admin/saved-replies/` · `POST` / `DELETE /api/admin/saved-replies/{id}/` | `{"title", "body"}` |
| `GET /api/admin/approvals/` · `POST …/approvals/{id}/approve/` · `POST …/approvals/{id}/reject/` | Pending and recent requests. Approving needs a different administrator from the requester. Rejecting your own request cancels it. Body for reject: `{"note"?}`. |
| `GET /api/admin/security/` · `POST …/security/totp/setup/` · `…/totp/enable/` · `…/totp/disable/` | Your own two-step sign-in. Setup returns `{"secret", "otpauth_uri"}`; enable takes `{"code"}`; disable takes `{"password", "code"}`. |
| `POST /api/admin/users/{id}/totp-reset/` | Turn off another administrator's two-step sign-in (signs them out). |

`AdminActivity` = `Activity` + `{user_id, admin_id, admin_username, approved_by_id, approved_by_username}`

#### Balance changes

```http
POST /api/admin/users/7/balance/
X-CSRF-Token: …
Idempotency-Key: 3f1c9a52-…            (optional, 8–64 chars of [A-Za-z0-9_-])
Content-Type: application/json

{"mode": "set",    "target_cents": 25000, "reason": "Account balance correction"}
{"mode": "adjust", "amount_cents": -2500, "reason": "Balance adjustment"}
```

- Amounts must be JSON **integers** (cents). Floats, strings, booleans and `null` are rejected.
- `|amount| ≤ 100,000,000,000` cents ($1,000,000,000.00), and the resulting balance must stay in the same range.
- An `adjust` of `0` is rejected. A `set` to the current balance is rejected with `no_change`.
- Sending `target_cents` with `adjust`, or `amount_cents` with `set`, is ambiguous and rejected. So is any extra field, such as a client-supplied `previous_cents`.
- Only accounts with the `user` role hold balances.
- A successful change returns **201** `{"user": UserSummary, "activity": AdminActivity, "previous_cents", "amount_cents", "resulting_cents", "replayed": false}`.
- If the change is at or above the approval threshold, nothing is applied yet. It returns **202** `{"pending": PendingChange}` and waits on the Approvals page. For `set`, the size is the difference from the current balance.
- Repeating the same `Idempotency-Key` with the same payload returns **200** with the original result and `"replayed": true`; nothing is applied twice. Reusing the key with a different payload returns **409** `idempotency_mismatch`.

## Authentication and roles

- **Sessions:** the signed cookie stores only the user ID, a session version and the CSRF token. Each request reloads the account from the database and checks that it still exists, is **active**, and has the same session version.
- **Deactivation:** deactivating an account bumps its session version, which ends all of its sessions immediately. Reactivating it does not bring old sessions back.
- **Pages vs APIs:** an unauthenticated browser is redirected (303) to `/login?next=…`. An unauthenticated API call gets 401 JSON. An account holder who requests an admin page gets the 403 page, and an administrator who opens `/dashboard` is redirected to `/admin`.
- **Inactive accounts:** an inactive account sees the generic "account unavailable" message, but only after entering the correct password, so the message cannot be used to probe accounts.
- **Logout:** `GET /logout` shows a confirmation page and never signs you out by itself. Signing out happens through `POST /logout` with a CSRF token; the header's **Sign out** button does this.

## Balance and activity rules

1. The administrator's role and status are checked, and the payload is validated.
2. Inside one transaction, the account row's write lock is taken, then the **current** balance is read. Any balance the client displayed is ignored.
3. The new balance is computed in integer cents, and the negative-balance and range policies are applied.
4. The balance and `updated_at` are updated, guarded by `WHERE balance_cents = <value read>`.
5. Exactly one `balance_activity` row is inserted: `amount_cents = resulting - previous`, `admin_id` = the signed-in administrator.
6. The transaction commits, and only then does the API report success. Any failure rolls back both the balance and the activity row.

## Negative balances

`ALLOW_NEGATIVE=0` (the default) rejects any set or adjust that would make the balance negative, with **409** `negative_balance`. Set `ALLOW_NEGATIVE=1` and restart to allow it. The policy is applied in one place, the balance service, and the admin UI's preview mirrors it.

## Optional demo data

Set `SEED_DEMO=1` and restart. The app then creates the fictional accounts `demo.avery`, `demo.jordan` and `demo.riley` if they don't already exist. Each starts at $0.00 with a generated temporary password that is printed **once** to the server console. Existing records are never changed. Set `SEED_DEMO=0` (the default) to turn seeding off.

## Display conventions

- **Currency:** USD with two decimals (`$1,250.00`). Negative amounts are shown as `−$25.50`, and changes as `+$10.00` or `−$10.00`.
- **Times:** stored in UTC and shown in the **browser's local time zone**. The activity tables name that zone, and hovering over a time shows its UTC value.

## Known limitations

- **Sign-up has no email verification** by design (a school project that asks for no personal data). Abuse is limited by a per-IP sign-up limit (10 per hour), a contact-form limit (5 per 10 minutes) and the admin's registration switch.
- **Replies typed in the admin panel** are not emailed. Account holders read them on their Support page. To answer by email, reply to the notification in the support mailbox, which goes to the submitter when they gave an address.
- **Rate limiting** is in memory and per process. It resets on restart and is not shared between Uvicorn workers. Run a single worker locally.
- **Logout** clears the cookie in that browser. Because sessions are signed cookies rather than server-side records, a copied cookie stays valid until it expires (14 days), unless the account is deactivated, which revokes every session.
- **Password reset** is self-service only for account holders who created recovery codes. Everyone else (and every administrator) asks through Contact or Support, and an administrator issues a temporary password.
- **Two-step sign-in** has no backup codes. An administrator who loses their device needs another administrator to reset it. If you run a single administrator, save the setup key when you turn it on.
- **Stale claims** are closed when an administrator clicks the button. There is no background scheduler.
- **SQLite** serialises writes. That suits a local single-user deployment; for multi-user production, move to PostgreSQL (the balance service's lock-then-read pattern maps to `SELECT … FOR UPDATE`).
- **Schema migrations** are not automated (see *Database initialisation*).
- **Single currency (USD) only.**

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ConfigError: SECRET_KEY is not configured` at startup | Create `.env` from `.env.example` and set a random `SECRET_KEY` of 32+ characters. |
| `ConfigError: No administrator exists yet…` | Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` (12+ characters) in `.env` and restart. |
| Admin password in `.env` doesn't work | The admin was created on first run with the password set at that time; later changes to `.env` are ignored. To start over, stop the server and move `ledger.db` aside. **This discards all data.** |
| "Your sign-in form expired" | The page was open before a restart or session change. Reload `/login` and try again. |
| HTTP 429 on sign-in | Too many failed attempts from your IP. Wait for the window (5 minutes by default) or restart the server. |
| `/api/...` returns 404 | Add the trailing slash: `/api/me/`, not `/api/me`. |
| `ValueError: password cannot be longer than 72 bytes` or bcrypt `__about__` warnings | Install the pinned `bcrypt==4.0.1`; newer bcrypt releases are incompatible with passlib 1.7.4. |
| `Address already in use` | Use `./run.sh`, which replaces an old RefundClaimers server automatically. If another program owns the port, run `PORT=8020 ./run.sh`. |
| `database is locked` | Another program has `ledger.db` open with a long write transaction. Close it and try again. |
