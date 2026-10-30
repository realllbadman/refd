/* Administrator pages: overview dashboard, activity ledger, messages, announcements,
 * audit log and settings. Every permission is enforced by the server. */
(function () {
  "use strict";
  const L = window.Ledger;
  const { el, $, $all, api, toast, setBusy, showError, fieldErrors, confirmAction, stateBlock, pagination,
    formatCents, formatDate, timeEl, amountCell } = L;

  function errorsFrom(err) {
    const out = {};
    (err.details || []).forEach((d) => { if (d.field && !out[d.field]) out[d.field] = err.message; });
    return out;
  }

  function avatar(name) {
    return el("span", { class: "avatar", "aria-hidden": "true", text: (name || "?").slice(0, 1).toUpperCase() });
  }

  // ---- overview dashboard ---------------------------------------------------------------

  function initOverviewDash() {
    async function loadStats() {
      const box = $("[data-stats]");
      try {
        const s = await api("/api/admin/stats/");
        const set = (k, v) => { const n = $('[data-stat="' + k + '"]'); if (n) n.textContent = v; };
        ["total_users", "active_users", "new_users_7d", "unread_messages", "recent_activity_count", "total_activity_count"]
          .forEach((k) => set(k, s[k].toLocaleString("en-US")));
        set("total_balance_display", formatCents(s.total_balance_cents));
        set("average_balance_display", formatCents(s.average_balance_cents));
        $("[data-stat-meta=inactive_users]").textContent = s.inactive_users + " inactive";
      } catch (err) {
        $all("[data-stat]").forEach((n) => { n.textContent = "—"; });
        showError($("[data-stats-error]"), "Statistics unavailable: " + err.message);
      } finally {
        box.setAttribute("aria-busy", "false");
      }
    }

    async function loadCharts(days) {
      const signupsBox = $('[data-chart="signups"]');
      const netBox = $('[data-chart="net"]');
      signupsBox.replaceChildren(stateBlock("loading", "Loading chart…"));
      netBox.replaceChildren(stateBlock("loading", "Loading chart…"));
      try {
        const data = await api("/api/admin/charts/?days=" + days);
        const short = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
        const long = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
        window.LedgerCharts.columns(signupsBox, {
          title: "New sign-ups per day",
          data: data.series.map((d) => ({ label: short(d.date), tip: long(d.date), value: d.signups })),
          format: (v) => v + (v === 1 ? " account" : " accounts"),
          valueLabel: "New accounts",
          emptyText: "No new accounts in this period.",
        });
        window.LedgerCharts.columns(netBox, {
          title: "Net balance change per day",
          data: data.series.map((d) => ({
            label: short(d.date),
            tip: long(d.date) + " · " + d.changes + " change" + (d.changes === 1 ? "" : "s"),
            value: d.net_cents / 100,
          })),
          colorFor: (v) => (v >= 0 ? "var(--chart-pos)" : "var(--chart-neg)"),
          format: (v) => formatCents(Math.round(v * 100), { signed: true }),
          axisFormat: (v) => (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0, notation: Math.abs(v) >= 10000 ? "compact" : "standard" }),
          valueLabel: "Net change",
          emptyText: "No balance changes in this period.",
        });
      } catch (err) {
        signupsBox.replaceChildren(stateBlock("error", "Chart unavailable", err.message));
        netBox.replaceChildren(stateBlock("error", "Chart unavailable", err.message));
      }
    }

    function feedEmpty(list, text) { list.replaceChildren(el("li", { class: "feed__empty", text })); }

    async function loadFeeds() {
      const activity = $('[data-feed="activity"]');
      const signups = $('[data-feed="signups"]');
      const top = $('[data-feed="top"]');
      try {
        const data = await api("/api/admin/overview/");
        if (!data.recent_activity.length) feedEmpty(activity, "No balance changes yet.");
        else activity.replaceChildren(...data.recent_activity.map((a) => el("li", { class: "feed__item" },
          avatar(a.username),
          el("div", { class: "feed__main" },
            el("a", { class: "strong-link", href: "/admin/users/" + a.user_id, text: a.username || ("#" + a.user_id) }),
            el("span", { class: "feed__sub" }, (a.change_type === "set" ? "Balance set" : "Adjustment") + " · " + L.relativeTime(a.created_at))),
          amountCell(a.amount_cents))));
        if (!data.recent_signups.length) feedEmpty(signups, "No accounts yet.");
        else signups.replaceChildren(...data.recent_signups.map((u) => el("li", { class: "feed__item" },
          avatar(u.username),
          el("div", { class: "feed__main" },
            el("a", { class: "strong-link", href: "/admin/users/" + u.id, text: u.display_name || u.username }),
            el("span", { class: "feed__sub", text: "@" + u.username + " · " + (u.signup_source === "self" ? "signed up " : "created ") + L.relativeTime(u.created_at) })),
          el("span", { class: "badge badge--" + u.status, text: u.status === "active" ? "Active" : "Inactive" }))));
        if (!data.top_balances.length) feedEmpty(top, "No positive balances yet.");
        else {
          const max = Math.max(...data.top_balances.map((u) => u.balance_cents));
          top.replaceChildren(...data.top_balances.map((u, i) => el("li", { class: "feed__item feed__item--rank" },
            el("span", { class: "rank", text: String(i + 1) }),
            el("div", { class: "feed__main" },
              el("a", { class: "strong-link", href: "/admin/users/" + u.id, text: u.username }),
              el("span", { class: "meter", "aria-hidden": "true" }, el("span", { class: "meter__fill", style: null, dataset: { w: String(Math.round((u.balance_cents / max) * 100)) } }))),
            el("span", { class: "num strong", text: formatCents(u.balance_cents) }))));
          $all(".meter__fill", top).forEach((n) => { n.style.width = n.dataset.w + "%"; });
        }
      } catch (err) {
        [activity, signups, top].forEach((list) => feedEmpty(list, "Couldn’t load: " + err.message));
      }
    }

    const group = $("[data-range-group]");
    group.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-range]");
      if (!btn) return;
      $all("[data-range]", group).forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
      loadCharts(Number(btn.dataset.range));
    });

    loadStats();
    loadCharts(30);
    loadFeeds();
  }

  // ---- activity ledger --------------------------------------------------------------------

  function initLedger() {
    const box = $("[data-ledger]");
    box.dataset.endpoint = "/api/admin/activity/";
    box.dataset.variant = "admin";
    box.dataset.pageSize = "25";
    box.dataset.emptyTitle = "No matching activity";
    box.dataset.emptyText = "Try different filters, or make a balance change from the Accounts page.";
    const table = L.ActivityTable(box, { showAccount: true });
    const form = $("[data-ledger-filters]");
    const exportLink = $("[data-export-link]");
    const apply = () => {
      const f = L.formValues(form);
      exportLink.href = "/api/admin/activity/export.csv" + L.qs(f);
      table.setFilters(f);
    };
    form.addEventListener("change", apply);
    form.q.addEventListener("input", L.debounce(apply, 300));
    form.addEventListener("submit", (e) => { e.preventDefault(); apply(); });
    table.load(1);
  }

  // ---- messages -----------------------------------------------------------------------------

  function initMessages() {
    const list = $("[data-msg-list]");
    const detail = $("[data-msg-detail]");
    const search = $("#m-q");
    let status = "open";
    let page = 1;
    let activeId = null;
    let items = [];

    async function load(p) {
      page = p || page;
      list.replaceChildren(stateBlock("loading", "Loading messages…"));
      try {
        const data = await api("/api/admin/messages/" + L.qs({ status, q: search.value.trim(), page, page_size: 20 }));
        items = data.items;
        if (!items.length) {
          list.replaceChildren(stateBlock("empty", "No messages here", status === "archived" ? "Archived messages will appear here." : "New contact and support messages will appear here."));
          return;
        }
        list.replaceChildren(el("ul", { class: "msg-list" }, items.map((m) => el("li", null,
          el("button", { type: "button", class: "msg-row" + (m.status === "new" ? " is-unread" : "") + (m.id === activeId ? " is-active" : ""),
            onclick: () => open(m.id) },
            el("span", { class: "msg-row__top" },
              el("strong", { text: m.name }),
              m.user_id ? el("span", { class: "chip chip--set", text: "Account holder" }) : el("span", { class: "chip", text: "Visitor" }),
              el("span", { class: "msg-row__date", text: L.relativeTime(m.created_at) })),
            el("span", { class: "msg-row__subject", text: m.subject }),
            el("span", { class: "msg-row__preview", text: m.body.slice(0, 90) + (m.body.length > 90 ? "…" : "") }))))),
          pagination(data, load, "Message pages") || "");
      } catch (err) {
        list.replaceChildren(stateBlock("error", "Couldn’t load messages", err.message));
      }
    }

    async function update(id, body, successText) {
      const m = await api("/api/admin/messages/" + id + "/", { method: "POST", body });
      if (successText) toast(successText, "success");
      const badge = document.querySelector(".sidebar__badge");
      if (badge && body.status && body.status !== "new") {
        const n = Math.max(0, Number(badge.textContent) - 1);
        if (n) badge.textContent = String(n); else badge.remove();
      }
      return m;
    }

    async function open(id) {
      activeId = id;
      let m = items.find((x) => x.id === id);
      if (!m) return;
      if (m.status === "new") {
        try { m = await update(id, { status: "read" }); } catch (_) { /* keep showing it */ }
      }
      renderDetail(m);
      load(page);
    }

    function renderDetail(m) {
      const reply = el("textarea", { class: "input textarea", rows: "5", maxlength: "4000", id: "reply-body",
        placeholder: m.user_id ? "Write a reply — the account holder sees it on their Support page." : "Write a note or reply (visitors without an account can’t see replies)." });
      reply.value = m.reply || "";
      const sendBtn = el("button", { type: "submit", class: "btn btn--primary" },
        el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { "data-label": true, text: m.reply ? "Update reply" : "Send reply" }));
      const form = el("form", { class: "form" },
        el("label", { class: "field__label", for: "reply-body", text: m.reply ? "Your reply" : "Reply" }), savedReplyPicker(reply), reply,
        el("div", { class: "button-row" }, sendBtn,
          m.status === "archived"
            ? el("button", { type: "button", class: "btn btn--secondary", onclick: async () => { renderDetail(await update(m.id, { status: "read" }, "Message restored.")); load(page); } }, "Restore")
            : el("button", { type: "button", class: "btn btn--secondary", onclick: async () => { renderDetail(await update(m.id, { status: "archived" }, "Message archived.")); load(page); } }, "Archive"),
          el("button", { type: "button", class: "btn btn--ghost", onclick: async () => { renderDetail(await update(m.id, { status: "new" }, "Marked as unread.")); load(page); } }, "Mark unread")));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!reply.value.trim() || sendBtn.disabled) return;
        setBusy(sendBtn, true, "Sending…");
        try {
          const updated = await update(m.id, { reply: reply.value }, "Reply saved.");
          renderDetail(updated);
          load(page);
        } catch (err) { toast(err.message, "error"); setBusy(sendBtn, false); }
      });
      detail.replaceChildren(
        el("header", { class: "msg-detail__head" },
          el("h2", { class: "section-title", text: m.subject }),
          el("p", { class: "muted small" },
            "From ", el("strong", { text: m.name }),
            m.username ? el("span", null, " (", el("a", { href: "/admin/users/" + m.user_id, text: "@" + m.username }), ")") : null,
            m.email ? " · " + m.email : "", " · ", timeEl(m.created_at)),
          el("span", { class: "chip", text: m.status === "new" ? "Unread" : m.status === "archived" ? "Archived" : "Read" })),
        el("div", { class: "msg-detail__body", text: m.body }),
        m.reply ? el("div", { class: "thread__reply" },
          el("p", { class: "thread__reply-label" }, "Replied · ", timeEl(m.replied_at)), el("p", { text: m.reply })) : "",
        form);
    }

    $("[data-msg-tabs]").addEventListener("click", (e) => {
      const tab = e.target.closest("[data-status]");
      if (!tab) return;
      status = tab.dataset.status;
      $all("[data-status]", e.currentTarget).forEach((t) => t.setAttribute("aria-selected", t === tab ? "true" : "false"));
      load(1);
    });
    search.addEventListener("input", L.debounce(() => load(1), 300));
    load(1);
  }

  // ---- saved replies (shared by claims and messages) -----------------------------------------

  let savedRepliesCache = null;
  async function loadSavedReplies(force) {
    if (!savedRepliesCache || force) {
      try { savedRepliesCache = await api("/api/admin/saved-replies/"); } catch (_) { savedRepliesCache = []; }
    }
    return savedRepliesCache;
  }

  /** A select that inserts a saved reply into `textarea`. */
  function savedReplyPicker(textarea) {
    const select = el("select", { class: "input claim-reply-template", "aria-label": "Insert a saved reply" },
      el("option", { value: "", text: "Insert saved reply…" }));
    loadSavedReplies().then((items) => {
      if (!items.length) {
        select.replaceChildren(el("option", { value: "", text: "No saved replies yet (add them in Settings)" }));
        select.disabled = true;
        return;
      }
      items.forEach((r) => select.append(el("option", { value: String(r.id), text: r.title })));
    });
    select.addEventListener("change", () => {
      const item = (savedRepliesCache || []).find((r) => String(r.id) === select.value);
      if (item) {
        textarea.value = textarea.value.trim() ? textarea.value.trimEnd() + "\n\n" + item.body : item.body;
        textarea.dispatchEvent(new Event("input"));
        textarea.focus();
      }
      select.value = "";
    });
    return select;
  }

  // ---- refund claims ----------------------------------------------------------------------

  const CLAIM_STATUS = {
    submitted: "Submitted",
    under_review: "Under review",
    awaiting_customer: "Awaiting customer",
    approved: "Approved",
    declined: "Declined",
    cancelled: "Cancelled",
  };
  const CLAIM_REASON = {
    not_received: "Order not received",
    damaged: "Item arrived damaged",
    incorrect_item: "Incorrect item",
    duplicate_charge: "Duplicate charge",
    cancelled: "Order cancelled",
    other: "Other",
  };
  const QUEUE_LABEL = { "": "All claims", mine: "Assigned to me", unassigned: "Unassigned", overdue: "No update in 48h", stale: "Stale (awaiting customer)" };

  function claimEventTitle(event) {
    if (event.event_type === "submitted") return "Claim submitted";
    if (event.event_type === "status_change") return "Status updated";
    if (event.event_type === "admin_reply") return "Reply to account holder";
    if (event.event_type === "customer_message") return "Customer update";
    if (event.event_type === "admin_note") return "Internal note";
    if (event.event_type === "evidence_added") return "Evidence uploaded";
    return "Claim update";
  }

  /** Compact age, e.g. "45m", "6h", "3d". */
  function ageText(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return minutes + "m";
    if (minutes < 1440) return Math.round(minutes / 60) + "h";
    return Math.round(minutes / 1440) + "d";
  }

  function durationText(minutes) {
    if (minutes === null || minutes === undefined) return "—";
    if (minutes < 60) return minutes + " min";
    if (minutes < 1440) return (minutes / 60).toFixed(minutes < 600 ? 1 : 0) + " h";
    return (minutes / 1440).toFixed(1) + " days";
  }

  function claimFlags(claim) {
    const flags = [];
    if (claim.possible_duplicate) flags.push(el("span", { class: "chip chip--warn", text: "Possible duplicate" }));
    if (claim.stale) flags.push(el("span", { class: "chip chip--warn", text: "Stale" }));
    else if (claim.overdue) flags.push(el("span", { class: "chip chip--warn", text: "No update 48h+" }));
    if (claim.credit_cents !== null && claim.credit_cents !== undefined) flags.push(el("span", { class: "chip chip--set", text: "Credited " + formatCents(claim.credit_cents) }));
    if (claim.pending_credit_cents) flags.push(el("span", { class: "chip", text: "Credit awaiting approval" }));
    return flags;
  }

  function initClaims() {
    const root = $("[data-admin-page]");
    const currentAdminId = Number(root.dataset.currentAdminId);
    const list = $(`[data-claim-queue]`);
    const detail = $(`[data-claim-detail]`);
    const queueFilter = $(`[data-claims-queue]`);
    const statusFilter = $(`[data-claims-status]`);
    const priorityFilter = $(`[data-claims-priority]`);
    const search = $(`[data-claims-search]`);
    const exportLink = $(`[data-claims-export]`);
    const closeStale = $(`[data-close-stale]`);
    let activeId = null;
    let page = 1;
    let assignees = null;

    function filters() {
      return { queue: queueFilter.value, status: statusFilter.value, priority: priorityFilter.value, q: search.value.trim() };
    }

    async function loadMetrics() {
      try {
        const m = await api("/api/admin/claims/metrics/");
        const set = (key, value) => { const node = $(`[data-metric="${key}"]`); if (node) node.textContent = value; };
        set("open", m.open.toLocaleString("en-US"));
        set("overdue_48h", m.overdue_48h.toLocaleString("en-US"));
        set("awaiting_customer", m.awaiting_customer.toLocaleString("en-US"));
        set("average_first_reply", durationText(m.average_first_reply_minutes));
        set("average_resolution", durationText(m.average_resolution_minutes));
        set("approval_rate", m.approval_rate_percent === null ? "—" : m.approval_rate_percent + "%");
        set("credited", formatCents(m.credited_cents) + (m.credited_count ? " · " + m.credited_count : ""));
        const reasons = $(`[data-insight-reasons]`);
        reasons.replaceChildren(m.by_reason.length ? el("table", { class: "table table--compact" },
          el("thead", null, el("tr", null, ["Reason", "Claims", "Approved", "Declined"].map((h) => el("th", { scope: "col", class: h === "Reason" ? null : "num", text: h })))),
          el("tbody", null, m.by_reason.map((r) => el("tr", null,
            el("td", { text: r.label }), el("td", { class: "num", text: String(r.total) }),
            el("td", { class: "num", text: String(r.approved) }), el("td", { class: "num", text: String(r.declined) })))))
          : el("p", { class: "muted small", text: "No claims yet." }));
        const merchants = $(`[data-insight-merchants]`);
        merchants.replaceChildren(m.top_merchants.length ? el("ol", { class: "insight-list" }, m.top_merchants.map((row) => el("li", null,
          el("span", { text: row.merchant }),
          el("span", { class: "muted small", text: row.total + (row.total === 1 ? " claim · " : " claims · ") + formatCents(row.amount_cents) }))))
          : el("p", { class: "muted small", text: "No claims yet." }));
      } catch (_) {
        $all("[data-metric]").forEach((node) => { node.textContent = "—"; });
      }
    }

    async function loadQueueCounts() {
      try {
        const counts = await api("/api/admin/claims/queues/");
        Array.from(queueFilter.options).forEach((option) => {
          const n = counts[option.value];
          option.textContent = QUEUE_LABEL[option.value] + (option.value && n !== undefined ? " (" + n + ")" : "");
        });
        closeStale.hidden = !counts.stale;
        closeStale.dataset.count = String(counts.stale);
        closeStale.dataset.days = String(counts.stale_after_days);
        $(`[data-close-stale-label]`).textContent = "Close " + counts.stale + " stale claim" + (counts.stale === 1 ? "" : "s");
      } catch (_) { /* counts are optional */ }
    }

    function refreshSummary() { loadMetrics(); loadQueueCounts(); }

    async function load(nextPage) {
      page = nextPage || page;
      exportLink.href = "/api/admin/claims/export.csv" + L.qs(filters());
      list.replaceChildren(stateBlock("loading", "Loading claims…"));
      try {
        const data = await api("/api/admin/claims/" + L.qs({ ...filters(), page, page_size: 20 }));
        if (!data.items.length) {
          list.replaceChildren(stateBlock("empty", "No matching claims", "Try a different queue, status, priority or search."));
          return;
        }
        const rows = data.items.map((claim) => {
          const button = el("button", {
            type: "button",
            class: "claim-queue__row" + (claim.id === activeId ? " is-active" : ""),
            onclick: () => openClaim(claim.id),
          },
          el("span", { class: "claim-queue__top" },
            el("strong", { text: claim.display_name || claim.username || ("Account #" + claim.user_id) }),
            el("span", { class: "claim-state claim-state--" + claim.status, text: CLAIM_STATUS[claim.status] || claim.status })),
          el("span", { class: "claim-queue__merchant", text: claim.merchant + " · " + claim.order_reference }),
          el("span", { class: "claim-queue__bottom" },
            el("span", { class: "chip" + (claim.priority === "urgent" ? " chip--warn" : ""), text: claim.priority }),
            el("span", { class: "muted small", title: "Time in current status", text: "in status " + ageText(claim.status_since || claim.created_at) }),
            claim.assigned_admin_name ? el("span", { class: "muted small", text: "· " + claim.assigned_admin_name }) : null,
            el("strong", { class: "num", text: formatCents(claim.amount_cents) })),
          claimFlags(claim).length ? el("span", { class: "claim-queue__flags" }, claimFlags(claim)) : null);
          return el("li", null, button);
        });
        list.replaceChildren(
          el("ul", { class: "claim-queue" }, rows),
          pagination(data, load, "Claim pages") || "");
      } catch (err) {
        list.replaceChildren(stateBlock("error", "Couldn’t load claims", err.message));
      }
    }

    async function openClaim(id) {
      activeId = id;
      detail.replaceChildren(stateBlock("loading", "Loading claim…"));
      try {
        if (!assignees) assignees = await api("/api/admin/claims/assignees/").catch(() => []);
        const claim = await api("/api/admin/claims/" + id + "/");
        renderDetail(claim);
        load(page);
      } catch (err) {
        detail.replaceChildren(stateBlock("error", "Couldn’t open claim", err.message));
      }
    }

    function afterChange(claim) {
      renderDetail(claim);
      load(page);
      refreshSummary();
    }

    function creditPanel(claim) {
      const terminal = ["approved", "declined", "cancelled"].includes(claim.status);
      if (terminal || claim.credit_cents !== null || claim.pending_credit_cents) return null;
      const amount = el("input", { class: "input mono", inputmode: "decimal", maxlength: "20", "aria-label": "Credit amount (USD)",
        value: (claim.amount_cents / 100).toFixed(2) });
      const reply = el("textarea", { class: "input textarea", rows: "2", maxlength: "4000",
        placeholder: "Optional message to the account holder" });
      const error = el("p", { class: "field__error", hidden: true });
      const button = el("button", { type: "submit", class: "btn btn--primary" },
        el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { "data-label": true, text: "Approve and credit" }));
      const form = el("form", { class: "claim-credit" },
        el("h3", { class: "claim-section-heading", text: "Approve and credit" }),
        el("p", { class: "muted small", text: "Closes the claim as approved and adds the credit to the account balance in one step. The ledger entry links back to this claim." }),
        el("div", { class: "claim-credit__row" },
          el("label", { class: "field" }, el("span", { class: "field__label", text: "Credit (claimed " + formatCents(claim.amount_cents) + ")" }),
            el("div", { class: "input-affix" }, el("span", { class: "input-affix__prefix", "aria-hidden": "true", text: "$" }), amount)),
          button),
        error,
        el("label", { class: "field" }, el("span", { class: "field__label", text: "Message to account holder" }), savedReplyPicker(reply), reply),
        el("p", { class: "field__hint", text: "To approve without a credit (for example, the merchant refunded directly), set the status to Approved in the review form below." }));
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const parsed = L.parseAmountToCents(amount.value, { allowSign: false });
        let message = "";
        if (!parsed.ok) message = parsed.error;
        else if (parsed.cents <= 0) message = "Enter an amount above zero.";
        else if (parsed.cents > claim.amount_cents) message = "The credit can’t exceed the claimed " + formatCents(claim.amount_cents) + ".";
        L.showError(error, message);
        if (message) return;
        confirmAction({
          title: "Approve and credit " + formatCents(parsed.cents) + "?",
          message: "Claim #" + claim.id + " will be approved and " + formatCents(parsed.cents) + " added to @" + claim.username +
            "’s balance. If this reaches the approval threshold it waits for a second administrator instead.",
          confirmLabel: "Approve and credit",
          action: async () => {
            const res = await api("/api/admin/claims/" + claim.id + "/credit/", { method: "POST", body: {
              credit_cents: parsed.cents, public_reply: reply.value.trim() || null,
            } });
            toast(res.pending ? "Credit sent for second-administrator approval." : "Claim approved and " + formatCents(parsed.cents) + " credited.", "success");
            afterChange(res.claim);
          },
        });
      });
      return form;
    }

    function assignmentRow(claim) {
      const select = el("select", { class: "input", "aria-label": "Assign to" },
        el("option", { value: "", text: claim.assigned_admin_id ? "Assigned" : "Unassigned", disabled: true, selected: !claim.assigned_admin_id }),
        (assignees || []).map((a) => el("option", { value: String(a.id), selected: a.id === claim.assigned_admin_id,
          text: (a.display_name || a.username) + (a.id === currentAdminId ? " (you)" : "") })));
      const save = async (body, text) => {
        try {
          const updated = await api("/api/admin/claims/" + claim.id + "/", { method: "POST", body });
          toast(text, "success");
          afterChange(updated);
        } catch (err) { toast(err.message, "error"); }
      };
      select.addEventListener("change", () => {
        if (select.value) save({ assign_to_user_id: Number(select.value) }, "Claim assigned.");
      });
      return el("div", { class: "claim-assign" },
        el("label", { class: "field" }, el("span", { class: "field__label", text: "Assigned to" }), select),
        claim.assigned_admin_id === currentAdminId ? null
          : el("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: () => save({ assign_to_me: true }, "Claim assigned to you.") }, "Assign to me"));
    }

    function duplicatesPanel(claim) {
      if (!claim.duplicates.length) return null;
      return el("div", { class: "alert alert--info claim-duplicates" },
        el("strong", { text: "Same merchant and order reference as " + claim.duplicates.length + (claim.duplicates.length === 1 ? " other claim" : " other claims") }),
        el("ul", null, claim.duplicates.map((d) => el("li", null,
          el("button", { type: "button", class: "link-btn", onclick: () => openClaim(d.id), text: "Claim #" + d.id }),
          " · @" + (d.username || d.user_id) + " · " + (CLAIM_STATUS[d.status] || d.status) + " · " + formatCents(d.amount_cents) + " · ",
          timeEl(d.created_at, "date")))));
    }

    function renderDetail(claim) {
      const timeline = el("ol", { class: "claim-timeline claim-timeline--admin" }, claim.events.map((event) => el("li", {
        class: "claim-timeline__event" + (event.event_type === "admin_note" ? " claim-timeline__event--internal" : ""),
      },
      el("span", { class: "claim-timeline__dot", "aria-hidden": "true" }),
      el("div", { class: "claim-timeline__content" },
        el("div", { class: "claim-timeline__head" },
          el("strong", { text: claimEventTitle(event) }),
          el("span", { class: "muted small" }, event.actor_label ? event.actor_label + " · " : "", timeEl(event.created_at))),
        event.from_status || event.to_status
          ? el("p", { class: "muted small", text: (CLAIM_STATUS[event.from_status] || "") + (event.from_status ? " → " : "") + (CLAIM_STATUS[event.to_status] || "") })
          : null,
        event.message ? el("p", { text: event.message }) : null))));

      const terminal = ["approved", "declined", "cancelled"].includes(claim.status);
      const statusOptions = terminal
        ? [claim.status]
        : ["under_review", "awaiting_customer", "approved", "declined", "cancelled"];
      if (claim.status === "submitted") statusOptions.unshift("submitted");
      const status = el("select", { class: "input", "aria-label": "Claim status" },
        ...statusOptions.map((value) =>
          el("option", { value, selected: claim.status === value, text: CLAIM_STATUS[value] + (value === "approved" && !terminal ? " (no credit)" : "") })));
      const priority = el("select", { class: "input", "aria-label": "Claim priority" },
        ...["normal", "high", "urgent"].map((value) => el("option", { value, selected: claim.priority === value, text: value[0].toUpperCase() + value.slice(1) })));
      const publicReply = el("textarea", { class: "input textarea", rows: "3", maxlength: "4000", placeholder: "Write an update visible to the account holder" });
      const internalNote = el("textarea", { class: "input textarea", rows: "3", maxlength: "4000", placeholder: "Private note for the review team" });
      const save = el("button", { type: "submit", class: "btn btn--primary" },
        el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { "data-label": true, text: "Save review" }));
      const form = el("form", { class: "claim-review-form" },
        el("div", { class: "claim-review-form__fields" },
          el("label", { class: "field" }, el("span", { class: "field__label", text: "Status" }), status),
          el("label", { class: "field" }, el("span", { class: "field__label", text: "Priority" }), priority)),
        el("label", { class: "field" }, el("span", { class: "field__label", text: "Reply to account holder" }), savedReplyPicker(publicReply), publicReply),
        el("label", { class: "field" }, el("span", { class: "field__label", text: "Internal note" }), internalNote),
        el("div", { class: "button-row" }, save));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        setBusy(save, true, "Saving…");
        try {
          const updated = await api("/api/admin/claims/" + claim.id + "/", { method: "POST", body: {
            status: status.value === claim.status ? null : status.value,
            priority: priority.value,
            public_reply: publicReply.value.trim() || null,
            internal_note: internalNote.value.trim() || null,
          } });
          toast("Claim review saved.", "success");
          afterChange(updated);
        } catch (err) { toast(err.message, "error"); }
        finally { setBusy(save, false, "Save review"); }
      });

      const evidence = claim.evidence.length
        ? el("div", { class: "claim-evidence" },
          el("h3", { class: "claim-section-heading", text: "Supporting documents" }),
          el("ul", { class: "claim-evidence__list" }, claim.evidence.map((item) => el("li", null,
            el("a", { href: item.download_path, text: item.original_filename, download: true }),
            el("span", { class: "muted small", text: Math.max(1, Math.round(item.size_bytes / 1024)) + " KB" })))))
        : null;

      // Optional sections are null; replaceChildren() would render them as the text "null".
      detail.replaceChildren(...[
        el("header", { class: "claim-detail__header" },
          el("div", null,
            el("p", { class: "eyebrow", text: "Claim #" + claim.id }),
            el("h2", { class: "section-title", text: claim.merchant })),
          el("span", { class: "claim-state claim-state--" + claim.status, text: CLAIM_STATUS[claim.status] || claim.status })),
        claimFlags(claim).length ? el("div", { class: "claim-queue__flags" }, claimFlags(claim)) : null,
        duplicatesPanel(claim),
        el("dl", { class: "claim-card__facts" },
          el("div", null, el("dt", { text: "Account" }), el("dd", null,
            el("a", { href: "/admin/users/" + claim.user_id, text: (claim.display_name || claim.username || "Account") + " · @" + claim.username }))),
          el("div", null, el("dt", { text: "Order" }), el("dd", { text: claim.order_reference })),
          el("div", null, el("dt", { text: "Purchase date" }), el("dd", { text: formatDate(claim.purchase_date + "T12:00:00") })),
          el("div", null, el("dt", { text: "Amount" }), el("dd", { text: formatCents(claim.amount_cents) })),
          el("div", null, el("dt", { text: "Reason" }), el("dd", { text: CLAIM_REASON[claim.reason] || claim.reason })),
          el("div", null, el("dt", { text: "In this status" }), el("dd", { text: ageText(claim.status_since || claim.created_at) }))),
        assignmentRow(claim),
        el("p", { class: "claim-card__description", text: claim.description }),
        evidence,
        el("h3", { class: "claim-section-heading", text: "Review history" }), timeline,
        creditPanel(claim),
        form,
      ].filter(Boolean));
    }

    closeStale.addEventListener("click", () => {
      const count = Number(closeStale.dataset.count);
      confirmAction({
        title: "Close " + count + " stale claim" + (count === 1 ? "" : "s") + "?",
        message: "Claims awaiting the customer for more than " + closeStale.dataset.days + " days will be cancelled, with a note telling the account holder they can start a new claim.",
        confirmLabel: "Close claims",
        danger: true,
        action: async () => {
          const res = await api("/api/admin/claims/close-stale/", { method: "POST" });
          toast(res.closed + " claim" + (res.closed === 1 ? "" : "s") + " closed.", "success");
          load(1);
          refreshSummary();
          if (activeId) openClaim(activeId);
        },
      });
    });
    [queueFilter, statusFilter, priorityFilter].forEach((node) => node.addEventListener("change", () => load(1)));
    search.addEventListener("input", L.debounce(() => load(1), 250));
    refreshSummary();
    load(1);
    const requested = Number(new URLSearchParams(location.search).get("claim"));
    if (requested) openClaim(requested);
  }

  // ---- announcements -------------------------------------------------------------------------

  function initAnnouncements() {
    const form = $("[data-ann-form]");
    const list = $("[data-ann-list]");
    const cancel = $("[data-ann-cancel]");
    let cache = [];

    function resetForm() {
      form.reset();
      form.id.value = "";
      fieldErrors(form, {});
      $("[data-ann-form-title]").textContent = "New announcement";
      $("[data-label]", $("[data-submit]", form)).textContent = "Publish";
      cancel.hidden = true;
    }

    function edit(a) {
      form.id.value = a.id;
      form.title.value = a.title;
      form.body.value = a.body;
      form.level.value = a.level;
      form.published.checked = a.published;
      form.pinned.checked = a.pinned;
      $("[data-ann-form-title]").textContent = "Edit announcement";
      $("[data-label]", $("[data-submit]", form)).textContent = "Save changes";
      cancel.hidden = false;
      form.title.focus();
    }

    async function load() {
      list.replaceChildren(stateBlock("loading", "Loading…"));
      try {
        cache = await api("/api/admin/announcements/");
        if (!cache.length) {
          list.replaceChildren(stateBlock("empty", "No announcements yet", "Write one on the left — it will appear on every account holder’s dashboard."));
          return;
        }
        list.replaceChildren(el("ul", { class: "ann-list" }, cache.map((a) => el("li", { class: "announcement announcement--" + a.level },
          el("div", { class: "announcement__head" },
            el("strong", { text: a.title }),
            a.pinned ? el("span", { class: "chip", text: "Pinned" }) : null,
            el("span", { class: "chip" + (a.published ? " chip--set" : ""), text: a.published ? "Published" : "Draft" }),
            el("span", { class: "announcement__date", text: formatDate(a.created_at) })),
          el("p", { text: a.body }),
          el("div", { class: "row-actions row-actions--start" },
            el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => edit(a) }, "Edit"),
            el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => save({ ...a, published: !a.published }, a.id) }, a.published ? "Unpublish" : "Publish"),
            el("button", { type: "button", class: "btn btn--danger-outline btn--xs", onclick: () => remove(a) }, "Delete"))))));
      } catch (err) {
        list.replaceChildren(stateBlock("error", "Couldn’t load announcements", err.message));
      }
    }

    async function save(values, id) {
      const body = { title: values.title, body: values.body, level: values.level, published: !!values.published, pinned: !!values.pinned };
      const res = await api("/api/admin/announcements/" + (id ? id + "/" : ""), { method: "POST", body });
      toast(id ? "Announcement updated." : "Announcement published.", "success");
      load();
      return res;
    }

    function remove(a) {
      confirmAction({
        title: "Delete this announcement?",
        message: "“" + a.title + "” will be removed from every dashboard. This can’t be undone.",
        confirmLabel: "Delete", danger: true,
        action: async () => {
          await api("/api/admin/announcements/" + a.id + "/", { method: "DELETE" });
          toast("Announcement deleted.", "success");
          if (String(form.id.value) === String(a.id)) resetForm();
          load();
        },
      });
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", form);
      const v = L.formValues(form);
      const errors = {};
      if (!v.title.trim()) errors.title = "Enter a title.";
      if (!v.body.trim()) errors.body = "Enter a message.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length || button.disabled) return;
      setBusy(button, true);
      try {
        await save(v, v.id || null);
        resetForm();
      } catch (err) {
        fieldErrors(form, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
    cancel.addEventListener("click", resetForm);
    load();
  }

  // ---- audit log -------------------------------------------------------------------------------

  const ACTION_TONE = { balance: "chip--set", account: "", auth: "", settings: "chip--warn", announcement: "", message: "" };

  function initAudit() {
    const box = $("[data-audit]");
    const form = $("[data-audit-filters]");
    let page = 1;
    async function load(p) {
      page = p || page;
      box.replaceChildren(stateBlock("loading", "Loading events…"));
      try {
        const data = await api("/api/admin/audit/" + L.qs({ ...L.formValues(form), page, page_size: 25 }));
        if (!data.items.length) { box.replaceChildren(stateBlock("empty", "No events", "Nothing matches these filters.")); return; }
        const rows = data.items.map((e) => el("tr", null,
          el("td", { "data-label": "When" }, timeEl(e.created_at)),
          el("td", { "data-label": "Event" }, el("span", { class: "chip " + (ACTION_TONE[e.action.split(".")[0]] || ""), text: e.action_label })),
          el("td", { "data-label": "By" }, e.actor_username
            ? el("a", { class: "strong-link", href: "/admin/users/" + e.actor_id, text: e.actor_username })
            : el("span", { class: "muted", text: "System" })),
          el("td", { "data-label": "Account" }, e.target_username && e.target_user_id !== e.actor_id
            ? el("a", { href: "/admin/users/" + e.target_user_id, text: e.target_username })
            : el("span", { class: "muted", text: "—" })),
          el("td", { "data-label": "Details", class: "reason" }, e.details || el("span", { class: "muted", text: "—" })),
          el("td", { "data-label": "IP", class: "mono small" }, e.ip || "—")));
        box.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table table--stack" },
          el("caption", { class: "visually-hidden", text: "Audit events" }),
          el("thead", null, el("tr", null, ["When", "Event", "By", "Account", "Details", "IP"].map((h) => el("th", { scope: "col", text: h })))),
          el("tbody", null, rows))), pagination(data, load, "Audit pages") || "");
      } catch (err) {
        box.replaceChildren(stateBlock("error", "Couldn’t load the audit log", err.message));
      }
    }
    form.addEventListener("change", () => load(1));
    form.q.addEventListener("input", L.debounce(() => load(1), 300));
    form.addEventListener("submit", (e) => { e.preventDefault(); load(1); });
    load(1);
  }

  // ---- settings -------------------------------------------------------------------------------------

  function initSettings() {
    const form = $("[data-settings-form]");
    const button = $("[data-submit]", form);
    const review = $("[data-review-settings-form]");
    const reviewButton = $("[data-submit]", review);
    function fillReview(s) {
      review.approval_threshold.value = s.approval_threshold_cents ? (s.approval_threshold_cents / 100).toFixed(2) : "0.00";
      review.stale_claim_days.value = String(s.stale_claim_days);
    }
    api("/api/admin/settings/").then((s) => {
      form.registration_open.checked = s.registration_open;
      form.public_notice.value = s.public_notice;
      form.setAttribute("aria-busy", "false");
      fillReview(s);
      review.setAttribute("aria-busy", "false");
    }).catch((err) => toast(err.message, "error"));
    review.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (reviewButton.disabled) return;
      const errors = {};
      const threshold = L.parseAmountToCents(review.approval_threshold.value || "0", { allowSign: false });
      if (!threshold.ok) errors.approval_threshold = threshold.error;
      const days = Number(review.stale_claim_days.value);
      if (!Number.isInteger(days) || days < 1 || days > 365) errors.stale_claim_days = "Enter a whole number of days from 1 to 365.";
      fieldErrors(review, errors);
      if (Object.keys(errors).length) return;
      setBusy(reviewButton, true, "Saving…");
      try {
        const s = await api("/api/admin/settings/", { method: "POST", body: { approval_threshold_cents: threshold.cents, stale_claim_days: days } });
        fillReview(s);
        toast(s.approval_threshold_cents
          ? "Saved. Changes of " + formatCents(s.approval_threshold_cents) + " or more now need a second administrator."
          : "Saved. Second-admin approval is off.", "success");
      } catch (err) {
        fieldErrors(review, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(reviewButton, false, "Save");
      }
    });
    initSavedReplies();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (button.disabled) return;
      setBusy(button, true, "Saving…");
      try {
        const s = await api("/api/admin/settings/", {
          method: "POST", body: { registration_open: form.registration_open.checked, public_notice: form.public_notice.value },
        });
        form.public_notice.value = s.public_notice;
        toast("Settings saved. Sign-ups are " + (s.registration_open ? "open." : "closed."), "success");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        setBusy(button, false, "Save settings");
      }
    });
  }

  // ---- saved replies manager (settings page) ----------------------------------------------------

  function initSavedReplies() {
    const list = $("[data-saved-replies]");
    const form = $("[data-saved-reply-form]");
    const button = $("[data-submit]", form);
    const cancel = $("[data-saved-reply-cancel]");

    function reset() {
      form.reset();
      form.id.value = "";
      fieldErrors(form, {});
      $("[data-label]", button).textContent = "Add saved reply";
      cancel.hidden = true;
    }

    async function load() {
      const items = await loadSavedReplies(true);
      if (!items.length) { list.replaceChildren(el("li", { class: "muted small", text: "No saved replies yet." })); return; }
      list.replaceChildren(...items.map((r) => el("li", { class: "saved-reply" },
        el("div", { class: "saved-reply__main" },
          el("strong", { text: r.title }),
          el("span", { class: "muted small", text: r.body.slice(0, 110) + (r.body.length > 110 ? "…" : "") })),
        el("div", { class: "row-actions" },
          el("button", { type: "button", class: "btn btn--secondary btn--xs", onclick: () => {
            form.id.value = r.id; form.title.value = r.title; form.body.value = r.body;
            $("[data-label]", button).textContent = "Save changes"; cancel.hidden = false; form.title.focus();
          } }, "Edit"),
          el("button", { type: "button", class: "btn btn--danger-outline btn--xs", onclick: () => confirmAction({
            title: "Delete this saved reply?", message: "“" + r.title + "” will no longer be offered when replying.",
            confirmLabel: "Delete", danger: true,
            action: async () => {
              await api("/api/admin/saved-replies/" + r.id + "/", { method: "DELETE" });
              toast("Saved reply deleted.", "success");
              if (String(form.id.value) === String(r.id)) reset();
              load();
            },
          }) }, "Delete")))));
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (button.disabled) return;
      const errors = {};
      if (!form.title.value.trim()) errors.title = "Enter a title.";
      if (!form.body.value.trim()) errors.body = "Enter the reply text.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length) return;
      setBusy(button, true, "Saving…");
      const id = form.id.value;
      try {
        await api("/api/admin/saved-replies/" + (id ? id + "/" : ""), { method: "POST", body: { title: form.title.value, body: form.body.value } });
        toast(id ? "Saved reply updated." : "Saved reply added.", "success");
        reset();
        load();
      } catch (err) {
        fieldErrors(form, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(button, false, id ? "Save changes" : "Add saved reply");
      }
    });
    cancel.addEventListener("click", reset);
    load();
  }

  // ---- second-administrator approvals ----------------------------------------------------------

  function initApprovals() {
    const currentAdminId = Number($("[data-admin-page]").dataset.currentAdminId);
    const pendingBox = $("[data-approvals-pending]");
    const recentBox = $("[data-approvals-recent]");
    const STATUS = { approved: "Approved", rejected: "Rejected", cancelled: "Cancelled", pending: "Pending" };

    function describe(p) {
      return p.mode === "set" ? "Set to " + formatCents(p.value_cents) : "Adjust " + formatCents(p.value_cents, { signed: true });
    }
    function resulting(p) {
      return p.mode === "set" ? p.value_cents : p.current_balance_cents + p.value_cents;
    }
    function accountCell(p) {
      return el("td", { "data-label": "Account" },
        el("a", { class: "strong-link", href: "/admin/users/" + p.user_id, text: p.username || ("#" + p.user_id) }),
        p.claim_id ? el("a", { class: "claim-link", href: "/admin/claims?claim=" + p.claim_id, text: "Claim #" + p.claim_id }) : null);
    }

    function table(caption, head, rows) {
      return el("div", { class: "table-wrap" }, el("table", { class: "table table--stack" },
        el("caption", { class: "visually-hidden", text: caption }),
        el("thead", null, el("tr", null, head.map((h) => el("th", { scope: "col", class: ["Change", "Balance now", "Result"].includes(h) ? "num" : null, text: h })))),
        el("tbody", null, rows)));
    }

    function decide(p, approve) {
      const own = p.requested_by_id === currentAdminId;
      if (approve) {
        confirmAction({
          title: "Approve this change?",
          message: describe(p) + " for @" + p.username + ". The balance is now " + formatCents(p.current_balance_cents) +
            " and will become " + formatCents(resulting(p)) + ".",
          confirmLabel: "Approve",
          action: async () => {
            await api("/api/admin/approvals/" + p.id + "/approve/", { method: "POST" });
            toast("Change approved and applied.", "success");
            load();
          },
        });
        return;
      }
      confirmAction({
        title: own ? "Cancel your request?" : "Reject this change?",
        message: own ? "The change will not be applied." : "The change will not be applied. The requesting administrator can see that you rejected it.",
        confirmLabel: own ? "Cancel request" : "Reject",
        danger: true,
        action: async () => {
          await api("/api/admin/approvals/" + p.id + "/reject/", { method: "POST", body: {} });
          toast(own ? "Request cancelled." : "Change rejected.", "success");
          load();
        },
      });
    }

    async function load() {
      pendingBox.replaceChildren(stateBlock("loading", "Loading…"));
      recentBox.replaceChildren();
      try {
        const data = await api("/api/admin/approvals/");
        const badge = $('.sidebar__link[href="/admin/approvals"] .sidebar__badge');
        if (badge && !data.pending.length) badge.remove();
        else if (badge) badge.textContent = String(data.pending.length);
        pendingBox.replaceChildren(data.pending.length
          ? table("Changes waiting for approval", ["Requested", "Account", "Change", "Balance now", "Result", "Reason", "By", ""],
            data.pending.map((p) => el("tr", null,
              el("td", { "data-label": "Requested" }, timeEl(p.created_at)),
              accountCell(p),
              el("td", { "data-label": "Change", class: "num", text: describe(p) }),
              el("td", { "data-label": "Balance now", class: "num", text: formatCents(p.current_balance_cents) }),
              el("td", { "data-label": "Result", class: "num strong", text: formatCents(resulting(p)) }),
              el("td", { "data-label": "Reason", class: "reason" }, p.reason || el("span", { class: "muted", text: "—" })),
              el("td", { "data-label": "By", text: p.requested_by_username || "" }),
              el("td", { "data-label": "Actions" }, el("div", { class: "row-actions" },
                p.requested_by_id === currentAdminId
                  ? el("span", { class: "muted small", text: "Needs another admin" })
                  : el("button", { type: "button", class: "btn btn--primary btn--xs", onclick: () => decide(p, true) }, "Approve"),
                el("button", { type: "button", class: "btn btn--danger-outline btn--xs", onclick: () => decide(p, false) },
                  p.requested_by_id === currentAdminId ? "Cancel" : "Reject"))))))
          : stateBlock("empty", "Nothing waiting", "Balance changes at or above the threshold will appear here."));
        recentBox.replaceChildren(data.recent.length
          ? table("Recent decisions", ["Decided", "Account", "Change", "Outcome", "Requested by", "Decided by", "Note"],
            data.recent.map((p) => el("tr", null,
              el("td", { "data-label": "Decided" }, p.decided_at ? timeEl(p.decided_at) : "—"),
              accountCell(p),
              el("td", { "data-label": "Change", class: "num", text: describe(p) }),
              el("td", { "data-label": "Outcome" }, el("span", { class: "chip" + (p.status === "approved" ? " chip--set" : ""), text: STATUS[p.status] || p.status })),
              el("td", { "data-label": "Requested by", text: p.requested_by_username || "" }),
              el("td", { "data-label": "Decided by", text: p.decided_by_username || "" }),
              el("td", { "data-label": "Note", class: "reason" }, p.decision_note || el("span", { class: "muted", text: "—" })))))
          : el("p", { class: "muted small card__padded-text", text: "No decisions yet." }));
      } catch (err) {
        pendingBox.replaceChildren(stateBlock("error", "Couldn’t load approvals", err.message));
      }
    }
    load();
  }

  // ---- my security (two-step sign-in) ------------------------------------------------------------

  function initSecurity() {
    const off = $("[data-totp-off]");
    const start = $("[data-totp-start]");
    const enable = $("[data-totp-enable]");
    const disable = $("[data-totp-disable]");
    const status = $("[data-totp-status]");

    function showState(on) {
      status.replaceChildren(el("span", { class: "badge badge--" + (on ? "active" : "inactive"), text: on ? "On" : "Off" }));
      off.hidden = on;
      disable.hidden = !on;
      enable.hidden = true;
      start.hidden = false;
      enable.reset();
      disable.reset();
    }

    start.addEventListener("click", async () => {
      setBusy(start, true);
      try {
        const res = await api("/api/admin/security/totp/setup/", { method: "POST" });
        $("[data-totp-secret]", enable).textContent = res.secret.replace(/(.{4})/g, "$1 ").trim();
        $("[data-totp-uri]", enable).href = res.otpauth_uri;
        enable.hidden = false;
        start.hidden = true;
        enable.code.focus();
      } catch (err) { toast(err.message, "error"); }
      finally { setBusy(start, false); }
    });

    enable.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", enable);
      if (button.disabled) return;
      setBusy(button, true, "Checking…");
      try {
        await api("/api/admin/security/totp/enable/", { method: "POST", body: { code: enable.code.value } });
        toast("Two-step sign-in is on. You’ll be asked for a code next time you sign in.", "success");
        showState(true);
      } catch (err) {
        fieldErrors(enable, errorsFrom(err));
        toast(err.message, "error");
      } finally { setBusy(button, false, "Turn on"); }
    });

    disable.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", disable);
      if (button.disabled) return;
      setBusy(button, true, "Turning off…");
      try {
        await api("/api/admin/security/totp/disable/", { method: "POST", body: { password: disable.password.value, code: disable.code.value } });
        toast("Two-step sign-in is off.", "success");
        showState(false);
      } catch (err) {
        fieldErrors(disable, errorsFrom(err));
        toast(err.message, "error");
      } finally { setBusy(button, false, "Turn off"); }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = $("[data-admin-page]");
    if (!root) return;
    const init = {
      "overview-dash": initOverviewDash, ledger: initLedger, messages: initMessages, claims: initClaims,
      announcements: initAnnouncements, audit: initAudit, settings: initSettings, approvals: initApprovals,
      security: initSecurity,
    }[root.dataset.adminPage];
    if (init) init();
  });
})();
