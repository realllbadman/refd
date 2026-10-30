/* Account-holder pages: overview, activity, support, profile. Own data only. */
(function () {
  "use strict";
  const L = window.Ledger;
  const { el, $, $all, api, toast, setBusy, fieldErrors, formatCents, formatDate, hydrateTimes, ActivityTable, stateBlock } = L;

  function errorsFrom(err) {
    const out = {};
    (err.details || []).forEach((d) => { if (d.field && !out[d.field]) out[d.field] = err.message; });
    return out;
  }

  // ---- overview ---------------------------------------------------------------------

  async function loadAnnouncements() {
    const box = $("[data-announcements]");
    try {
      const items = await api("/api/user/announcements/");
      if (!items.length) { box.hidden = true; return; }
      box.replaceChildren(...items.map((a) => el("article", { class: "announcement announcement--" + a.level },
        el("div", { class: "announcement__head" },
          el("strong", { text: a.title }),
          a.pinned ? el("span", { class: "chip", text: "Pinned" }) : null,
          el("span", { class: "announcement__date", text: formatDate(a.created_at) })),
        el("p", { text: a.body }))));
      box.hidden = false;
    } catch (_) {
      box.hidden = true; // announcements are optional; never block the dashboard
    }
  }

  async function loadClaimOverview() {
    const box = $(`[data-claim-overview]`);
    if (!box) return;
    try {
      const claims = await api("/api/user/claims/");
      const open = claims.filter((claim) => !["approved", "declined", "cancelled"].includes(claim.status));
      const awaiting = open.filter((claim) => claim.status === "awaiting_customer").length;
      const summary = open.length
        ? open.length + (open.length === 1 ? " open claim" : " open claims") + (awaiting ? " · " + awaiting + " need your reply" : "")
        : "No open claims. View your claim history or start a new request.";
      $(`[data-claim-overview-summary]`).textContent = summary;
      box.hidden = false;
    } catch (_) {
      box.hidden = true;
    }
  }

  async function loadSummary() {
    try {
      const s = await api("/api/user/summary/");
      $("[data-balance]").textContent = formatCents(s.balance_cents);
      $("[data-sum=credits]").textContent = formatCents(s.total_credits_cents);
      $("[data-sum=debits]").textContent = formatCents(Math.abs(s.total_debits_cents));
      $("[data-sum=count]").textContent = s.change_count.toLocaleString("en-US");
      const chartBox = $("[data-balance-chart]");
      if (window.LedgerCharts && chartBox) {
        window.LedgerCharts.line(chartBox, {
          title: "Balance after each change",
          points: s.history.map((h) => ({ label: formatDate(h.at), tip: L.formatDateTime(h.at), value: h.balance_cents / 100 })),
          format: (v) => formatCents(Math.round(v * 100)),
          axisFormat: (v) => "$" + (Math.abs(v) >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + "K" : v),
          valueLabel: "Balance",
          emptyText: "Your balance chart appears once a couple of changes have been recorded.",
        });
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function initOverview() {
    const activity = ActivityTable($("[data-activity]"));
    activity.load(1);
    loadAnnouncements();
    loadSummary();
    const refresh = $("[data-refresh]");
    refresh.addEventListener("click", async () => {
      setBusy(refresh, true);
      try {
        const me = await api("/api/me/");
        const updated = $("[data-balance-updated]");
        updated.dataset.ts = me.updated_at;
        hydrateTimes(updated.parentNode);
        await Promise.all([loadSummary(), activity.reset(), loadAnnouncements()]);
        toast("Up to date.", "success");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        setBusy(refresh, false);
      }
    });
  }

  // ---- activity ------------------------------------------------------------------------

  function initActivity() {
    const activity = ActivityTable($("[data-activity]"));
    const form = $("[data-activity-filters]");
    form.addEventListener("change", () => activity.setFilters(L.formValues(form)));
    activity.load(1);
  }

  // ---- support ----------------------------------------------------------------------------

  const STATUS_LABEL = { new: "Sent", read: "Seen by team", archived: "Closed" };

  async function loadMessages() {
    const box = $("[data-messages]");
    box.replaceChildren(stateBlock("loading", "Loading messages…"));
    try {
      const items = await api("/api/user/messages/");
      if (!items.length) {
        box.replaceChildren(stateBlock("empty", "No messages yet", "Messages you send will appear here with any replies."));
        return;
      }
      if (items.some((m) => m.reply_unread)) {
        api("/api/user/messages/seen/", { method: "POST" }).then(() => {
          const badge = document.querySelector('.sidebar__link[href="/dashboard/support"] .sidebar__badge');
          if (badge) badge.remove();
        }).catch(() => {});
      }
      box.replaceChildren(el("ul", { class: "thread-list" }, items.map((m) => el("li", { class: "thread" + (m.reply_unread ? " has-update" : "") },
        el("div", { class: "thread__head" },
          el("strong", { text: m.subject }),
          m.reply_unread ? el("span", { class: "chip chip--set", text: "New reply" }) : null,
          el("span", { class: "chip" + (m.reply ? " chip--set" : ""), text: m.reply ? "Replied" : STATUS_LABEL[m.status] || m.status }),
          el("span", { class: "thread__date" }, L.timeEl(m.created_at))),
        el("p", { class: "thread__body", text: m.body }),
        m.reply ? el("div", { class: "thread__reply" },
          el("p", { class: "thread__reply-label" }, "Reply from the team · ", L.timeEl(m.replied_at)),
          el("p", { text: m.reply })) : null))));
    } catch (err) {
      box.replaceChildren(stateBlock("error", "Couldn’t load messages", err.message,
        el("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: loadMessages }, "Try again")));
    }
  }

  function initSupport() {
    const form = $("[data-support-form]");
    const button = $("[data-submit]", form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (button.disabled) return;
      const errors = {};
      if (!form.subject.value.trim()) errors.subject = "Enter a subject.";
      if (!form.body.value.trim()) errors.body = "Enter a message.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length) return;
      setBusy(button, true, "Sending…");
      try {
        await api("/api/user/messages/", { method: "POST", body: { subject: form.subject.value, body: form.body.value, email: form.email.value.trim() || null } });
        form.reset();
        form.body.dispatchEvent(new Event("input"));
        toast("Message sent. We’ll reply here soon.", "success");
        loadMessages();
      } catch (err) {
        fieldErrors(form, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(button, false, "Send message");
      }
    });
    loadMessages();
  }

  // ---- profile ------------------------------------------------------------------------------

  function initProfile() {
    const profile = $("[data-profile-form]");
    profile.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", profile);
      setBusy(button, true, "Saving…");
      try {
        const user = await api("/api/user/profile/", { method: "POST", body: { display_name: profile.display_name.value } });
        fieldErrors(profile, {});
        $all("[data-header-name]").forEach((n) => { n.textContent = user.display_name || user.username; });
        toast("Profile saved.", "success");
      } catch (err) {
        fieldErrors(profile, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(button, false, "Save profile");
      }
    });

    const pw = $("[data-password-form]");
    pw.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("[data-submit]", pw);
      const errors = {};
      if (!pw.current_password.value) errors.current_password = "Enter your current password.";
      const bytes = new TextEncoder().encode(pw.new_password.value).length;
      if (pw.new_password.value.length < 8) errors.new_password = "Use at least 8 characters.";
      else if (bytes > 72) errors.new_password = "Use at most 72 bytes.";
      if (pw.new_password.value !== pw.confirm_password.value) errors.confirm_password = "Passwords do not match.";
      fieldErrors(pw, errors);
      if (Object.keys(errors).length) return;
      setBusy(button, true, "Updating…");
      try {
        const res = await api("/api/user/password/", {
          method: "POST", body: { current_password: pw.current_password.value, new_password: pw.new_password.value },
        });
        // The session was re-issued; keep using its new CSRF token.
        $('meta[name="csrf-token"]').setAttribute("content", res.csrf_token);
        $all('input[name="csrf_token"]').forEach((i) => { i.value = res.csrf_token; });
        pw.reset();
        toast("Password updated. Other devices have been signed out.", "success");
      } catch (err) {
        const errs = errorsFrom(err);
        if (errs.password) { errs.new_password = errs.password; }
        fieldErrors(pw, errs);
        toast(err.message, "error");
      } finally {
        setBusy(button, false, "Update password");
      }
    });
  }

  // ---- profile: recovery codes, sign-ins, closing ---------------------------------------------

  function initRecovery() {
    const form = $("[data-recovery-form]");
    const panel = $("[data-recovery-codes]");
    const button = $("[data-submit]", form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (button.disabled) return;
      if (!form.password.value) { fieldErrors(form, { password: "Enter your password." }); return; }
      setBusy(button, true, "Creating…");
      try {
        const res = await api("/api/user/recovery-codes/", { method: "POST", body: { password: form.password.value } });
        fieldErrors(form, {});
        form.reset();
        $("[data-recovery-list]", panel).replaceChildren(...res.codes.map((code) => el("li", { text: code })));
        panel.hidden = false;
        form.hidden = true;
        $("[data-recovery-status]").replaceChildren(el("span", { class: "badge badge--active", text: res.remaining + " unused" }));
      } catch (err) {
        fieldErrors(form, errorsFrom(err));
        toast(err.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
    $("[data-recovery-copy]", panel).addEventListener("click", () => {
      L.copyText($all("li", $("[data-recovery-list]", panel)).map((li) => li.textContent).join("\n"), "Recovery codes copied.");
    });
    $("[data-recovery-hide]", panel).addEventListener("click", () => {
      $("[data-recovery-list]", panel).replaceChildren();
      panel.hidden = true;
      form.hidden = false;
      $("[data-label]", button).textContent = "Replace my codes";
    });
  }

  async function loadSignIns() {
    const list = $("[data-signins]");
    try {
      const items = await api("/api/user/sign-ins/");
      list.replaceChildren(...(items.length ? items.map((s) => el("li", null, L.timeEl(s.created_at),
        el("span", { class: "muted small mono", text: s.ip || "" })))
        : [el("li", { class: "muted small", text: "No sign-ins recorded yet." })]));
    } catch (err) {
      list.replaceChildren(el("li", { class: "muted small", text: "Couldn’t load: " + err.message }));
    }
  }

  function initClose() {
    const form = $("[data-close-form]");
    const button = $("[data-submit]", form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (button.disabled) return;
      const errors = {};
      if (!form.password.value) errors.password = "Enter your password.";
      if (!form.confirm.checked) errors.confirm = "Tick the box to confirm.";
      fieldErrors(form, errors);
      if (Object.keys(errors).length) return;
      setBusy(button, true, "Closing…");
      try {
        const res = await api("/api/user/close/", { method: "POST", body: { password: form.password.value } });
        location.assign(res.redirect);
      } catch (err) {
        fieldErrors(form, errorsFrom(err));
        toast(err.message, "error");
        setBusy(button, false, "Close my account");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = $("[data-user-page]");
    if (!root) return;
    const profile = () => { initProfile(); initRecovery(); loadSignIns(); initClose(); };
    ({ overview: initOverview, activity: initActivity, support: initSupport, profile }[root.dataset.userPage] || (() => {}))();
  });
})();
